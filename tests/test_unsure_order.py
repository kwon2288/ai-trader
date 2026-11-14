"""응답 없는 주문(시간 초과) — 다시 보내지 않고, 계좌 수량으로 체결 여부를 확인해 장부에 반영."""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from trader import fills  # noqa: E402
from trader.bot import Bot  # noqa: E402
from trader.broker import Holding  # noqa: E402
from trader.journal import Journal  # noqa: E402
import test_bot as tb  # noqa: E402


class TimeoutBroker(tb.FakeBroker):
    def buy(self, code, qty, price=None):
        self.orders.append(("BUY", code, qty))
        raise TimeoutError("Read timed out")


def _bot(broker, mode="mock"):
    cfg = tb.make_cfg(Path(tempfile.mkdtemp()), mode=mode)
    b = Bot(cfg, broker, None, Journal(cfg.db_path))
    b.lines = []
    b.say = b.lines.append
    sent = []
    b.notify = lambda k, t: sent.append((k, t))
    b.orders_left = 5
    return b, sent


def test_timeout_is_not_resent_and_marked():
    br = TimeoutBroker({"000001": 10_000}, holdings={"000001": Holding("000001", "가", 3, 9_000, 10_000)})
    b, sent = _bot(br)
    b.acct_qty = {"000001": 3}                              # 사용자가 원래 3주 갖고 있음
    assert b._exec("BUY", "000001", "가", 2, 10_000, "AI") is False
    assert len(br.orders) == 1 and "000001" in b.paused and sent and "응답 없음" in sent[0][1]
    items = fills.unsure_list(b.j, b.cfg)
    assert items[0]["before"] == 3 and items[0]["qty"] == 2 and "timed out" in items[0]["error"]
    assert b.j.db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0   # 확인 전엔 주문 기록 없음


def test_resolve_filled_buy_partial_and_pending():
    br = TimeoutBroker({"000001": 10_000})
    b, sent = _bot(br)
    cfg, j = b.cfg, b.j
    fills.mark_unsure(j, cfg, "BUY", "000001", "가", 2, 10_000, "AI", 3, "timeout")
    fills.mark_unsure(j, cfg, "BUY", "000002", "나", 1, 5_000, "AI", 0, "timeout")
    hold = {"000001": Holding("000001", "가", 5, 9_400, 10_000)}             # 3 → 5주: 체결됨
    out = fills.resolve_unsure(j, cfg, hold, b.say, b.notify)
    assert out == {"000002"}                                                  # 나: 오늘이라 아직 대기
    assert j.bot_codes(cfg.market).get("000001") and j.bot_lots(cfg.market, cfg.ledger)["000001"][0] == 2
    o = dict(j.db.execute("SELECT * FROM orders").fetchone())
    assert o["status"] == "filled" and o["qty"] == 2 and "응답 없음" in o["trigger"] and o["order_no"] == "UNSURE"
    assert any("실제로 체결" in t for _, t in sent)
    items = fills.unsure_list(j, cfg)                                         # 거래일이 지나면 ‘접수 안 됨’으로 정리
    items[0]["day"] = "2000-01-01"
    j.set(f"unsure_{cfg.ledger}", json.dumps(items))
    assert fills.resolve_unsure(j, cfg, hold, b.say, b.notify) == set()
    assert fills.unsure_list(j, cfg) == [] and any("접수되지 않은" in t for _, t in sent)


def test_resolve_sell():
    b, sent = _bot(tb.FakeBroker({"000001": 10_000}))
    cfg, j = b.cfg, b.j
    j.open_position("000001", "가", cfg.market, 4, 9_000)
    fills.mark_unsure(j, cfg, "SELL", "000001", "가", 4, 10_000, "손절", 4, "timeout")
    assert fills.resolve_unsure(j, cfg, {}, b.say) == set()                  # 계좌에서 사라짐 → 매도 체결
    assert "000001" not in j.bot_codes(cfg.market)
    assert j.db.execute("SELECT side FROM orders").fetchone()[0] == "SELL"


def test_dry_mode_does_not_mark():
    b, _ = _bot(TimeoutBroker({"000001": 10_000}), mode="dry")
    assert b._exec("BUY", "000001", "가", 1, 10_000, "AI") is False
    assert fills.unsure_list(b.j, b.cfg) == []


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
