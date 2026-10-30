"""알림 — 텔레그램 전송, 카카오 토큰 갱신·state 확인, 주문 알림, 하루 요약 1회."""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.update(TRADER_DATA_DIR=tempfile.mkdtemp(), WEB_PASSWORD="secret-pass", TRADING_MODE="mock")

from trader import notify  # noqa: E402

sent = []


class Resp:
    def __init__(self, data, ok=True):
        self.data, self.ok, self.status_code, self.text = data, ok, 200 if ok else 400, json.dumps(data)

    def json(self):
        return self.data


def fake_post(url, json=None, data=None, headers=None, timeout=None):
    sent.append((url, json or data, headers))
    if "oauth/token" in url:
        return Resp({"access_token": "AT2", "expires_in": 21599})
    return Resp({"ok": True})


notify.requests.post = fake_post


class Cfg:
    def __init__(self, d, n=None):
        self.data_dir, self.notify = Path(d), n or {}


def test_telegram_and_toggles():
    sent.clear()
    os.environ.update(TELEGRAM_BOT_TOKEN="T", TELEGRAM_CHAT_ID="42")
    c = Cfg(tempfile.mkdtemp(), {"orders": False})
    assert notify.send(c, "orders", "x") == [] and not sent        # 꺼진 종류는 안 보냄
    notify.send(c, "errors", "에러")                                 # 기본 켜짐
    assert sent and "botT/sendMessage" in sent[0][0] and sent[0][1]["chat_id"] == "42"
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        os.environ.pop(k)


def test_kakao_state_and_refresh():
    sent.clear()
    d = Path(tempfile.mkdtemp())
    os.environ["KAKAO_REST_KEY"] = "K"
    url = notify.kakao_auth_url(d, "https://stock.example.com/kakao/callback")
    assert "client_id=K" in url and "talk_message" in url
    try:
        notify.kakao_callback(d, "code", "WRONG")
        raise AssertionError("state 검증 안 됨")
    except RuntimeError:
        pass
    url = notify.kakao_auth_url(d, "https://stock.example.com/kakao/callback")
    state = url.split("state=")[1]
    notify.kakao_callback(d, "code", state)
    t = json.loads((d / "kakao_token.json").read_text())
    assert t["access_token"] == "AT2" and t["link"] == "https://stock.example.com"
    t.update(access_exp=time.time() - 1, refresh_token="RT", refresh_exp=time.time() + 86400 * 30)
    (d / "kakao_token.json").write_text(json.dumps(t))
    sent.clear()
    notify.kakao_send(d, "안녕")
    assert any("grant_type" in str(x[1]) and x[1].get("grant_type") == "refresh_token" for x in sent)
    msg = [x for x in sent if "memo/default/send" in x[0]][0]
    assert json.loads(msg[1]["template_object"])["text"] == "안녕"
    os.environ.pop("KAKAO_REST_KEY")


def test_bot_order_notifies():
    sys.path.insert(0, str(Path(__file__).parent))
    import test_bot as tb
    msgs = []
    notify.send = lambda cfg, kind, text, force=False: msgs.append((kind, text)) or []
    _, br, _, bot = tb.setup(decisions=[
        {"code": "000001", "action": "BUY", "amount_krw": 40_000, "confidence": 0.9, "reason": "r"}])
    bot.run()
    assert any(k == "orders" and "매수" in t and "종목1" in t for k, t in msgs), msgs


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
