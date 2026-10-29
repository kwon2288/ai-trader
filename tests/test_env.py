"""웹 '연결 정보' — .env 고쳐 쓰기, 비밀번호 확인, 값 숨김."""
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
tmp = Path(tempfile.mkdtemp())
os.environ.update(TRADER_DATA_DIR=str(tmp / "data"), WEB_PASSWORD="secret-pass", TRADING_MODE="dry")

from trader import envfile  # noqa: E402

ENV = tmp / ".env"
ENV.write_text("""# 설명
TRADING_MODE=dry
NHPLUG_APP_KEY=OLDKEY1234
NHPLUG_APP_SECRET=oldsecret
# GEMINI_API_KEY=
WEB_PASSWORD=secret-pass
""", encoding="utf-8")
envfile.ENV_PATH = ENV
envfile.update.__defaults__ = (ENV,)
envfile.read.__defaults__ = (ENV,)
envfile.ensure.__defaults__ = (ENV,)

from web.app import app  # noqa: E402

app.config["TESTING"] = True


def client():
    c = app.test_client()
    with c.session_transaction() as s:
        s["ok"] = True
    return c


def test_envfile_update_keeps_comments():
    p = tmp / "x.env"
    p.write_text("# a\nA=1\n# B=\nC=3   # note\n", encoding="utf-8")
    envfile.update({"A": "2", "B": "bee", "C": None, "D": "has space"}, p)
    t = p.read_text(encoding="utf-8")
    assert t == '# a\nA=2\nB=bee\nD="has space"\n', t
    assert (tmp / "x.env.bak").exists()
    assert oct(p.stat().st_mode)[-3:] == "600"
    assert envfile.read(p) == {"A": "2", "B": "bee", "D": "has space"}


def test_page_never_shows_values():
    html = client().get("/settings").get_data(as_text=True)
    assert "oldsecret" not in html and "OLDKEY1234" not in html and "secret-pass" not in html
    assert "•••• 1234" in html and "설정됨" in html


def test_wrong_password_saves_nothing():
    before = ENV.read_text(encoding="utf-8")
    client().post("/env", data={"current_password": "nope", "GEMINI_API_KEY": "g-key", "BROKER": "nh"})
    assert ENV.read_text(encoding="utf-8") == before


def test_save_blank_keeps_and_clear_removes():
    r = client().post("/env", data={"current_password": "secret-pass", "BROKER": "nh", "BROKER_US": "",
                                    "GEMINI_API_KEY": "g-key", "NHPLUG_APP_KEY": "",
                                    "clear_NHPLUG_APP_SECRET": "on"})
    assert "저장했습니다" in r.get_data(as_text=True)
    v = envfile.read()
    assert v["GEMINI_API_KEY"] == "g-key" and v["NHPLUG_APP_KEY"] == "OLDKEY1234"
    assert "NHPLUG_APP_SECRET" not in v and v["BROKER"] == "nh"
    assert os.environ["GEMINI_API_KEY"] == "g-key"
    assert "# GEMINI_API_KEY" not in ENV.read_text(encoding="utf-8")   # 예시 줄을 살려 씀


def test_bad_values_and_password_change():
    before = ENV.read_text(encoding="utf-8")
    client().post("/env", data={"current_password": "secret-pass", "BROKER": "evil", "GEMINI_API_KEY": "a\\nB=1"})
    client().post("/env", data={"current_password": "secret-pass", "BROKER": "nh", "new_password": "short",
                                "new_password2": "short"})
    assert ENV.read_text(encoding="utf-8") == before
    client().post("/env", data={"current_password": "secret-pass", "BROKER": "nh",
                                "new_password": "new-pass-123", "new_password2": "new-pass-123"})
    assert envfile.read()["WEB_PASSWORD"] == "new-pass-123" and os.environ["WEB_PASSWORD"] == "new-pass-123"
    os.environ["WEB_PASSWORD"] = "secret-pass"
    envfile.update({"WEB_PASSWORD": "secret-pass"})


def test_protected_keys_not_editable():
    client().post("/env", data={"current_password": "secret-pass", "BROKER": "nh",
                                "I_UNDERSTAND_REAL_MONEY": "yes", "WEB_LIVE_SWITCH": "on", "WEB_ENV_EDIT": "on"})
    v = envfile.read()
    assert not any(k in v for k in ("I_UNDERSTAND_REAL_MONEY", "WEB_LIVE_SWITCH", "WEB_ENV_EDIT"))


def test_env_edit_off():
    os.environ["WEB_ENV_EDIT"] = "off"
    try:
        before = ENV.read_text(encoding="utf-8")
        client().post("/env", data={"current_password": "secret-pass", "BROKER": "kis"})
        assert ENV.read_text(encoding="utf-8") == before
        assert "WEB_ENV_EDIT=off" in client().get("/settings").get_data(as_text=True)
    finally:
        os.environ.pop("WEB_ENV_EDIT")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
