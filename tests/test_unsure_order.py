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


class FindBroker(tb.FakeBroker):
    """NH 국내처럼 주문번호 없이 주문을 찾을 수 있는 증권사."""
    def __init__(self, *a, found=None, **kw):
        super().__init__(*a, **kw)
        self.found = found or []

    def find_orders(self, code, side, qty, day):
        return [(x[0], x[1]) for x in self.found if x[2] == code]

    def order_status(self, order_no, code, side, qty, price, day):
        return None

    def cancel(self, order_no, code):
        return None


def _fill(n, px, state="filled"):
    from trader.broker import OrderFill
    return OrderFill(n, px, 0, state)


def test_nh_lookup_single_match_records_order():
    """NH 국내 주문번호 누락 → 체결 내역에서 찾아 그 주문번호로 기록 (체결 수량·가격은 체결 확인이 증권사 값으로)."""
    br = FindBroker({"000001": 10_000}, found=[("12345", _fill(2, 10_050), "000001")])
    b, sent = _bot(br)
    fills.mark_unsure(b.j, b.cfg, "BUY", "000001", "가", 2, 10_000, "AI", 0, "timeout")
    out = fills.resolve_unsure(b.j, b.cfg, {}, b.say, b.notify, br)
    assert out == {"000001"} and fills.unsure_list(b.j, b.cfg) == []          # 이번 실행은 쉬고, 목록에선 뺌
    o = dict(b.j.db.execute("SELECT * FROM orders").fetchone())
    assert o["order_no"] == "12345" and o["status"] == "sent" and "증권사 확인" in o["trigger"]
    assert any("주문 내역에서 찾았어요" in t for _, t in sent)


def test_nh_lookup_two_matches_not_confirmed():
    """같은 조건 주문 2건(사용자가 같은 수량을 직접 주문) → 자동 확정 안 함."""
    br = FindBroker({"000001": 10_000}, found=[("1", _fill(2, 10_000), "000001"), ("2", _fill(2, 10_010), "000001")])
    b, sent = _bot(br)
    fills.mark_unsure(b.j, b.cfg, "BUY", "000001", "가", 2, 10_000, "AI", 0, "timeout")
    assert fills.resolve_unsure(b.j, b.cfg, {}, b.say, b.notify, br) == {"000001"}
    assert b.j.db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
    assert any("2건" in t for _, t in sent)
    n = len(sent)
    fills.resolve_unsure(b.j, b.cfg, {}, b.say, b.notify, br)                     # 알림은 한 번만
    assert len(sent) == n


def test_nh_lookup_excludes_known_bot_orders_and_rejected():
    from trader.broker import OrderResult
    br = FindBroker({"000001": 10_000}, found=[("777", _fill(2, 10_000), "000001"), ("888", _fill(0, 0, "rejected"), "000001")])
    b, sent = _bot(br)
    b.j.add_order(b.cfg.ledger, "BUY", "000001", "가", 2, 10_000, OrderResult(True, "777", ""), "AI", "filled")  # 이미 아는 주문
    fills.mark_unsure(b.j, b.cfg, "BUY", "000001", "가", 2, 10_000, "AI", 0, "timeout")
    assert fills.resolve_unsure(b.j, b.cfg, {}, b.say, b.notify, br) == set()
    assert any("거부·취소" in t for _, t in sent)


def test_user_manual_buy_not_mistaken_without_lookup():
    """조회 안 되는 증권사: 사용자가 직접 5주를 더 사서 수량이 7주 늘면 → 봇 2주 주문으로 확정하지 않음."""
    b, sent = _bot(tb.FakeBroker({"000001": 10_000}))
    fills.mark_unsure(b.j, b.cfg, "BUY", "000001", "가", 2, 10_000, "AI", 3, "timeout")
    hold = {"000001": Holding("000001", "가", 10, 9_400, 10_000)}             # 3 → 10주 (+7)
    assert fills.resolve_unsure(b.j, b.cfg, hold, b.say, b.notify) == {"000001"}
    assert b.j.db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
    assert not b.j.bot_codes(b.cfg.market) and any("확정하지 않았어요" in t for _, t in sent)
    items = fills.unsure_list(b.j, b.cfg)                                      # 거래일 지나면 확정 안 한 채 정리
    items[0]["day"] = "2000-01-01"
    b.j.set(f"unsure_{b.cfg.ledger}", json.dumps(items))
    assert fills.resolve_unsure(b.j, b.cfg, hold, b.say, b.notify) == set() and fills.unsure_list(b.j, b.cfg) == []
    assert not b.j.bot_codes(b.cfg.market)


def test_exact_qty_change_is_estimated_and_marked():
    """조회 안 되는 증권사: 수량이 주문 수량만큼 정확히 늘면 ‘추정 체결’로 넣고 장부에서 구분."""
    b, sent = _bot(tb.FakeBroker({"000001": 10_000}))
    fills.mark_unsure(b.j, b.cfg, "BUY", "000001", "가", 10, 10_000, "AI", 0, "timeout")
    hold = {"000001": Holding("000001", "가", 10, 10_000, 10_000)}
    assert fills.resolve_unsure(b.j, b.cfg, hold, b.say, b.notify) == set()
    o = dict(b.j.db.execute("SELECT * FROM orders").fetchone())
    assert o["order_no"] == "ESTIMATED" and "추정" in o["trigger"] and o["qty"] == 10
    assert b.j.bot_lots(b.cfg.market, b.cfg.ledger)["000001"][0] == 10 and any("추정 체결" in t for _, t in sent)
    fills.mark_unsure(b.j, b.cfg, "BUY", "000002", "나", 1, 5_000, "AI", 0, "timeout")   # 수량 그대로 + 날짜 지남 → 정리
    items = fills.unsure_list(b.j, b.cfg)
    items[0]["day"] = "2000-01-01"
    b.j.set(f"unsure_{b.cfg.ledger}", json.dumps(items))
    assert fills.resolve_unsure(b.j, b.cfg, hold, b.say, b.notify) == set()
    assert any("접수되지 않은" in t for _, t in sent)


def test_resolve_sell():
    b, sent = _bot(tb.FakeBroker({"000001": 10_000}))
    cfg, j = b.cfg, b.j
    j.open_position("000001", "가", cfg.market, 4, 9_000)
    fills.mark_unsure(j, cfg, "SELL", "000001", "가", 4, 10_000, "손절", 4, "timeout")
    assert fills.resolve_unsure(j, cfg, {}, b.say) == set()                  # 계좌에서 4주 사라짐 → 매도 추정
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
