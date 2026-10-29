"""웹에서 실행 모드 전환 — 확인 절차, 연결 확인, 실계좌 허용 판단."""
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.update(TRADER_DATA_DIR=tempfile.mkdtemp(), WEB_PASSWORD="secret-pass", TRADING_MODE="mock", BROKER="nh")
os.environ.pop("I_UNDERSTAND_REAL_MONEY", None)

from trader import config as cfgmod, envfile, modefile, runner  # noqa: E402
from trader.broker import AccountChoice, Balance, BrokerError  # noqa: E402

_ENV = Path(os.environ["TRADER_DATA_DIR"]) / ".env"
for f in (envfile.update, envfile.read, envfile.ensure):
    f.__defaults__ = (_ENV,)

calls = []


class FakeBroker:
    def __init__(self, cfg):
        self.cfg, self.account = cfg, "LIVE-1" if cfg.mode == "live" else "MOCK-1"

    def balance(self):
        calls.append(self.cfg.mode)
        if os.environ.get("FAIL_LIVE") and self.cfg.mode == "live":
            raise BrokerError("인증 실패")
        if os.environ.get("MULTI_LIVE") and self.cfg.mode == "live" and not os.environ.get("NHPLUG_ACCOUNT_LIVE"):
            raise AccountChoice("여러 개", "live", ["111", "222"])
        return Balance(orderable_cash=500000, holdings={})


runner.make_broker = FakeBroker
import web.app as webapp  # noqa: E402
from web.app import app  # noqa: E402

webapp._describe_accounts = lambda cands, mode: [[a, "주문가능 1원"] for a in cands]

app.config["WTF_CSRF_ENABLED"] = False


def client():
    c = app.test_client()
    with c.session_transaction() as s:
        s["ok"] = True
    return c


def flashes(c):
    return re.findall(r'class="flash (\w+)">([^<]*)', c.get("/settings").get_data(as_text=True))


def d():
    return cfgmod.load().data_dir


def test_default_follows_env():
    modefile.clear(d())
    c = cfgmod.load()
    assert (c.mode, c.mode_source) == ("mock", "env")
    assert not runner.live_allowed(cfgmod.load().__class__(**{**c.__dict__, "mode": "live"}))


def test_live_needs_password_phrase_agree():
    modefile.clear(d())
    c = client()
    for form in ({"mode": "live", "password": "wrong", "phrase": "실계좌 전환", "agree": "on"},
                 {"mode": "live", "password": "secret-pass", "phrase": "실계좌", "agree": "on"},
                 {"mode": "live", "password": "secret-pass", "phrase": "실계좌 전환"}):
        c.post("/mode", data=form)
        assert cfgmod.load().mode == "mock", form
    assert "live" not in calls


def test_live_switch_ok_and_allowed():
    modefile.clear(d())
    c = client()
    c.post("/mode", data={"mode": "live", "password": "secret-pass", "phrase": "실계좌 전환", "agree": "on"})
    cfg = cfgmod.load()
    assert (cfg.mode, cfg.mode_source, cfg.live_ack) == ("live", "web", True)
    assert runner.live_allowed(cfg)
    assert any("LIVE-1" in m for _k, m in flashes(c))


def test_failed_connection_keeps_mode():
    modefile.clear(d())
    os.environ["FAIL_LIVE"] = "1"
    try:
        client().post("/mode", data={"mode": "live", "password": "secret-pass", "phrase": "실계좌 전환", "agree": "on"})
        assert cfgmod.load().mode == "mock"
    finally:
        os.environ.pop("FAIL_LIVE")


def test_back_to_mock_and_clear():
    c = client()
    c.post("/mode", data={"mode": "dry"})
    assert cfgmod.load().mode == "dry"
    c.post("/mode", data={"mode": "env"})
    assert (cfgmod.load().mode, cfgmod.load().mode_source) == ("mock", "env")


def test_toss_has_no_mock():
    os.environ["BROKER"] = "toss"
    try:
        modefile.clear(d())
        client().post("/mode", data={"mode": "mock"})
        assert cfgmod.load().mode_source == "env"   # 바뀌지 않음
    finally:
        os.environ["BROKER"] = "nh"


def test_web_live_switch_off():
    modefile.clear(d())
    os.environ["WEB_LIVE_SWITCH"] = "off"
    try:
        c = client()
        assert "🔴 실계좌 — 실제 돈" not in c.get("/settings").get_data(as_text=True)
        c.post("/mode", data={"mode": "live", "password": "secret-pass", "phrase": "실계좌 전환", "agree": "on"})
        assert cfgmod.load().mode == "mock"
    finally:
        os.environ.pop("WEB_LIVE_SWITCH")


def test_multiple_accounts_choose_one():
    modefile.clear(d())
    os.environ["MULTI_LIVE"] = "1"
    os.environ.pop("NHPLUG_ACCOUNT_LIVE", None)
    live = {"mode": "live", "password": "secret-pass", "phrase": "실계좌 전환", "agree": "on"}
    try:
        c = client()
        c.post("/mode", data=live)
        assert cfgmod.load().mode == "mock"
        html = c.get("/settings").get_data(as_text=True)
        assert 'name="account"' in html and "222" in html
        c.post("/mode", data={**live, "account": "999"})          # 후보에 없는 계좌는 거부
        assert cfgmod.load().mode == "mock" and "NHPLUG_ACCOUNT_LIVE" not in envfile.read()
        c.post("/mode", data=live)
        c.post("/mode", data={**live, "account": "222"})
        assert cfgmod.load().mode == "live"
        assert envfile.read()["NHPLUG_ACCOUNT_LIVE"] == "222"
        assert 'name="account"' not in c.get("/settings").get_data(as_text=True)
    finally:
        os.environ.pop("MULTI_LIVE")
        os.environ.pop("NHPLUG_ACCOUNT_LIVE", None)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
