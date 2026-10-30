"""오프라인 테스트 — 가짜 증권사·가짜 AI로 위험관리 규칙을 검증한다.

  python -m pytest tests -q     (또는 python tests/test_bot.py)
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trader.broker import Balance, Holding, OrderResult, Quote  # noqa: E402
from trader.config import Config  # noqa: E402
from trader.journal import Journal  # noqa: E402
from trader.bot import Bot  # noqa: E402


def make_cfg(tmp: Path, mode="mock", **kw) -> Config:
    base = dict(total_krw=100_000, max_per_position_krw=50_000, max_positions=2,
                stop_loss_pct=-7, take_profit_pct=15, max_drawdown_pct=-20,
                max_orders_per_day=4, min_confidence=0.6, min_hold_days=2,
                order_type="market", ai_provider="claude", ai_models={"claude": "x", "gemini": "y", "openai": "z"},
                ai_max_tokens=100,
                symbols=["000001", "000002", "000003"], mode=mode, data_dir=tmp, auto_count=0)
    base.update(kw)
    return Config(**base)


class FakeBroker:
    def __init__(self, prices, holdings=None, status="1", warnings=None):
        self.prices, self.status = prices, status
        self.holdings = holdings or {}
        self.warnings = warnings or {}
        self.orders = []

    def quote(self, code):
        p = self.prices[code]
        return Quote(code, f"종목{code[-1]}", p, 0.5, p, p - 10, self.status, "코스피", "",
                     10, 1, 5, p * 2, p // 2, self.warnings.get(code, []))

    def daily(self, code, n=80):
        p = self.prices[code]
        return [{"date": f"2026{i:04d}", "open": p, "high": p, "low": p,
                 "close": p - 40 + i // 2, "volume": 1000} for i in range(80)]

    def balance(self):
        return Balance(10_000_000, dict(self.holdings))

    def buyable_qty(self, code, price):
        return 10_000

    def sellable_qty(self, code):
        h = self.holdings.get(code)
        return h.qty if h else 0

    def buy(self, code, qty, price=None):
        self.orders.append(("BUY", code, qty))
        return OrderResult(True, "1001", "정상")

    def sell(self, code, qty, price=None):
        self.orders.append(("SELL", code, qty))
        return OrderResult(True, "1002", "정상")


class FakeAI:
    def __init__(self, decisions):
        self.decisions, self.ctx = decisions, None

    def decide(self, ctx):
        self.ctx = ctx
        return {"market_view": "테스트", "decisions": self.decisions}


def setup(mode="mock", decisions=(), prices=None, holdings=None, status="1", **kw):
    tmp = Path(tempfile.mkdtemp())
    cfg = make_cfg(tmp, mode, **kw)
    broker = FakeBroker(prices or {"000001": 20_000, "000002": 30_000, "000003": 80_000},
                        holdings, status=status)
    ai = FakeAI(list(decisions))
    return cfg, broker, ai, Bot(cfg, broker, ai, Journal(cfg.db_path))


def test_buy_respects_position_cap_and_budget():
    _, br, _, bot = setup(decisions=[
        {"code": "000001", "action": "BUY", "amount_krw": 999_999, "confidence": 0.8, "reason": "r"},
        {"code": "000002", "action": "BUY", "amount_krw": 50_000, "confidence": 0.7, "reason": "r"},
        {"code": "000003", "action": "BUY", "amount_krw": 50_000, "confidence": 0.9, "reason": "r"},
    ])
    out = bot.run()
    # 000003 은 8만원 > 종목당 5만원 → 1주도 못 삼. 000001 은 5만원 한도 → 2주(4만원)
    assert ("BUY", "000001", 2) in br.orders
    assert ("BUY", "000002", 1) in br.orders
    assert not any(o[1] == "000003" for o in br.orders)
    assert bot.avail == 100_000 - 40_000 - 30_000, out
    assert bot.j.bot_lots()["000001"] == (2, 20_000)


def test_low_confidence_and_dry_mode():
    _, br, _, bot = setup(decisions=[
        {"code": "000001", "action": "BUY", "amount_krw": 40_000, "confidence": 0.4, "reason": "r"}])
    bot.run()
    assert br.orders == []
    _, br, _, bot = setup(mode="dry", decisions=[
        {"code": "000001", "action": "BUY", "amount_krw": 40_000, "confidence": 0.9, "reason": "r"}])
    out = bot.run()
    assert ("BUY", "000001", 2) in br.orders  # FakeBroker는 그대로 받지만 장부는 안 바뀜
    assert bot.j.bot_codes() == {}, out


def test_stop_loss_runs_before_ai_and_only_bot_positions():
    holdings = {"000001": Holding("000001", "종목1", 2, 22_000, 20_000),   # -9% → 손절
                "000002": Holding("000002", "남의종목", 5, 10_000, 30_000)}  # 봇 종목 아님
    cfg, br, ai, bot = setup(holdings=holdings)
    bot.j.open_position("000001", "종목1")
    bot.run()
    assert ("SELL", "000001", 2) in br.orders
    assert not any(o[1] == "000002" and o[0] == "SELL" for o in br.orders)
    assert all(p["code"] != "000002" for p in ai.ctx["portfolio"]["positions"])


def test_min_hold_days_blocks_ai_sell():
    holdings = {"000001": Holding("000001", "종목1", 2, 20_000, 20_000)}
    _, br, _, bot = setup(holdings=holdings, decisions=[
        {"code": "000001", "action": "SELL", "sell_ratio": 1, "confidence": 0.9, "reason": "r"}])
    bot.j.open_position("000001", "종목1")  # 오늘 매수 → 보유 0일
    bot.run()
    assert br.orders == []


def test_drawdown_halts_and_kill_switch():
    cfg, br, _, bot = setup(decisions=[
        {"code": "000001", "action": "BUY", "amount_krw": 40_000, "confidence": 0.9, "reason": "r"}])
    from trader.broker import OrderResult as R
    bot.j.add_order("mock", "BUY", "000009", "x", 1, 100_000, R(True, "1", "ok"), "AI", status="filled")   # 10만원에 사서
    bot.j.add_order("mock", "SELL", "000009", "x", 1, 70_000, R(True, "2", "ok"), "AI", status="filled")   # 7만원에 팜 → -30%
    out = bot.run()
    assert br.orders == [] and "최대 손실" in out
    assert bot.j.get("halted_mock") == "1"
    cfg2, br2, _, bot2 = setup()
    cfg2.stop_file.write_text("x")
    assert "킬스위치" in bot2.run() and br2.orders == []


def test_market_closed_skips():
    tmp = Path(tempfile.mkdtemp())
    cfg = make_cfg(tmp)
    br = FakeBroker({"000001": 20_000, "000002": 30_000, "000003": 80_000}, status="2")
    bot = Bot(cfg, br, FakeAI([{"code": "000001", "action": "BUY", "confidence": 0.9, "reason": "r"}]),
              Journal(cfg.db_path))
    assert "정규장이 아닙니다" in bot.run() and br.orders == []


def test_daily_order_limit():
    _, br, _, bot = setup(max_orders_per_day=1, decisions=[
        {"code": "000001", "action": "BUY", "amount_krw": 20_000, "confidence": 0.9, "reason": "r"},
        {"code": "000002", "action": "BUY", "amount_krw": 30_000, "confidence": 0.8, "reason": "r"}])
    bot.run()
    assert len(br.orders) == 1


def test_warning_stock_rejected():
    tmp = Path(tempfile.mkdtemp())
    cfg = make_cfg(tmp)
    br = FakeBroker({"000001": 20_000, "000002": 30_000, "000003": 80_000},
                    warnings={"000001": ["단기과열"]})
    bot = Bot(cfg, br, FakeAI([{"code": "000001", "action": "BUY", "amount_krw": 40_000,
                                "confidence": 0.9, "reason": "r"}]), Journal(cfg.db_path))
    bot.run()
    assert br.orders == []


def test_user_holdings_untouched():
    # 사용자가 직접 가진 종목(000002 10주)은 사지도 팔지도 않는다
    _, br, ai, bot = setup(decisions=[
        {"code": "000002", "action": "BUY", "amount_krw": 30_000, "confidence": 0.9, "reason": "r"},
        {"code": "000002", "action": "SELL", "sell_ratio": 1, "confidence": 0.9, "reason": "r"}],
        holdings={"000002": Holding("000002", "종목2", 10, 20_000, 30_000)})
    out = bot.run()
    assert br.orders == [], out
    assert "사용자가 직접 보유" in out and "봇 보유 종목 아님" in out
    assert all(p["code"] != "000002" for p in ai.ctx["portfolio"]["positions"])


def test_bot_sells_only_its_own_shares():
    # 봇이 3주 산 뒤 사용자가 같은 종목을 7주 더 사서 계좌엔 10주 → 손절 때도 봇 몫 3주만 판다
    _, br, _, bot = setup(holdings={"000001": Holding("000001", "종목1", 10, 22_000, 20_000)})
    bot.j.open_position("000001", "종목1", "kr", 3, 25_000)      # 봇 평단 25,000 → 현재 20,000 = -20% 손절
    out = bot.run()
    assert ("SELL", "000001", 3) in br.orders and not any(o[2] > 3 for o in br.orders if o[1] == "000001"), out
    assert "봇 몫 3주만" in out
    assert "000001" not in bot.j.bot_codes()


def test_partial_sell_keeps_rest():
    _, br, _, bot = setup(decisions=[
        {"code": "000001", "action": "SELL", "sell_ratio": 0.5, "confidence": 0.9, "reason": "r"}],
        holdings={"000001": Holding("000001", "종목1", 4, 20_000, 20_500)})
    bot.j.open_position("000001", "종목1", "kr", 4, 20_000)
    bot.j.db.execute("UPDATE bot_positions SET opened='2026-01-01'")
    bot.run()
    assert ("SELL", "000001", 2) in br.orders
    assert bot.j.bot_lots()["000001"][0] == 2


def test_old_position_without_qty_uses_order_history():
    # v0.6.2 이전: 사용자 27주 + 봇 1주가 섞인 28주, 봇 장부엔 수량 없음 → 주문 기록으로 봇 몫 1주만
    from trader.broker import OrderResult as R
    _, br, _, bot = setup(holdings={"000001": Holding("000001", "종목1", 28, 15_000, 24_000)},
                          prices={"000001": 24_000, "000002": 30_000, "000003": 80_000})
    bot.j.db.execute("INSERT INTO bot_positions(code,name,opened,market) VALUES('000001','종목1','2026-01-01','kr')")
    bot.j.add_order("mock", "BUY", "000001", "종목1", 1, 20_000, R(True, "9", "ok"), "AI", status="filled")
    out = bot.run()
    assert "봇 몫 1주만" in out, out
    assert ("SELL", "000001", 1) in br.orders                         # 익절(+15%)도 1주만
    assert not any(o[2] > 1 for o in br.orders if o[1] == "000001")


def test_fixed_budget_limited_by_account_cash():
    # 예산 10만원, 봇 투자 중 2만원 → 남은 예산 8만원. 계좌 현금 3만원이면 3만원까지만
    from trader.broker import OrderResult as R
    _, br, _, bot = setup(decisions=[
        {"code": "000002", "action": "BUY", "amount_krw": 50_000, "confidence": 0.9, "reason": "r"}],
        holdings={"000001": Holding("000001", "종목1", 1, 20_000, 20_000)})
    bot.j.open_position("000001", "종목1", "kr", 1, 20_000)
    bot.broker.balance = lambda: Balance(30_000, dict(bot.broker.holdings))
    out = bot.run()
    assert ("BUY", "000002", 1) in br.orders, out          # 30,000원 → 1주(30,000)
    assert "30,000원까지만" in out


def test_profit_not_reinvested_and_pnl_sums():
    # 예산 고정: 3만원 벌어도 한도는 예산 그대로, 손익은 실현+평가 합산
    from trader import ledger
    from trader.broker import OrderResult as R
    cfg, br, _, bot = setup(holdings={"000001": Holding("000001", "종목1", 1, 20_000, 18_000)})
    j = bot.j
    cfg.costs = {"kr_fee_pct": 0, "kr_sell_tax_pct": 0}                 # 수수료 없이 계산 확인
    j.add_order("mock", "BUY", "000009", "x", 1, 50_000, R(True, "1", "ok"), "AI", status="filled")
    j.add_order("mock", "SELL", "000009", "x", 1, 80_000, R(True, "2", "ok"), "AI", status="filled")
    j.open_position("000001", "종목1", "kr", 1, 20_000)
    held = {"000001": Holding("000001", "종목1", 1, 20_000, 18_000)}
    st = ledger.status(j, cfg, held, 10_000_000)
    assert (st["realized"], st["unrealized"], st["pnl"]) == (30_000, -2_000, 28_000)
    assert st["pnl_pct"] == 28.0 and st["room"] == 80_000 and st["avail"] == 80_000


def test_fees_and_tax_reduce_realized_pnl():
    from trader import ledger
    from trader.broker import OrderResult as R
    cfg, _, _, bot = setup()
    j = bot.j
    j.add_order("mock", "BUY", "000009", "삼성전자", 10, 10_000, R(True, "1", "ok"), "AI", status="filled")
    j.add_order("mock", "SELL", "000009", "삼성전자", 10, 11_000, R(True, "2", "ok"), "AI", status="filled")
    j.add_order("mock", "BUY", "000008", "KODEX 200", 10, 10_000, R(True, "3", "ok"), "AI", status="filled")
    j.add_order("mock", "SELL", "000008", "KODEX 200", 10, 11_000, R(True, "4", "ok"), "AI", status="filled")
    t = {x["name"]: x for x in ledger.trades(j, "mock", ledger.costs_of(cfg))}
    # 주식: 수수료 0.015% ×(10만+11만) + 거래세 0.20% × 11만 = 31.5 + 220
    assert abs(t["삼성전자"]["pnl"] - (10_000 - 31.5 - 220)) < 0.01
    assert abs(t["KODEX 200"]["pnl"] - (10_000 - 31.5)) < 0.01        # ETF 는 거래세 없음
    # 증권사가 실제 수수료·세금을 주면 그 값을 쓴다
    j.db.execute("UPDATE orders SET fee=10, tax=0 WHERE order_no IN ('1','2')")
    t = {x["name"]: x for x in ledger.trades(j, "mock", ledger.costs_of(cfg))}
    assert abs(t["삼성전자"]["pnl"] - (10_000 - 20)) < 0.01


def test_fill_tracking_partial_and_cancel():
    # 체결 조회를 지원하는 증권사: 주문 즉시 보유로 잡지 않고, 체결된 만큼만 반영. 다음 실행 때 미체결은 취소
    from trader import fills
    from trader.broker import OrderFill
    _, br, _, bot = setup(decisions=[
        {"code": "000001", "action": "BUY", "amount_krw": 40_000, "confidence": 0.9, "reason": "r"}])
    state = {"filled": 1}
    br.order_status = lambda no, code, side, qty, price, day: OrderFill(
        state["filled"], 20_000, qty - state["filled"], "filled" if state["filled"] >= qty else
        ("canceled" if state.get("canceled") else "partial"))
    br.cancel = lambda no, code: (state.update(canceled=True), __import__("trader.broker", fromlist=["x"]).OrderResult(True, "C", "ok"))[1]
    fills.time.sleep = lambda s: None
    import trader.bot as tbot
    bot.run()
    assert bot.j.bot_lots()["000001"][0] == 1                  # 2주 주문 중 1주만 체결 → 1주만 보유
    o = bot.j.orders("mock")[0]
    assert o["status"] == "partial" and o["filled_qty"] == 1
    # 다음 실행: 남은 1주는 취소
    bot.j.db.execute("UPDATE orders SET ts = '2000-01-01T00:00:00' || substr(ts, 20)")
    bot.j.db.execute("UPDATE orders SET ts = datetime('now','localtime','-1 minute')")
    out = fills.reconcile(bot.j, bot.cfg, br, say=lambda m: None, cancel_before="9999")
    o = bot.j.orders("mock")[0]
    assert o["status"] == "canceled" and o["filled_qty"] == 1 and bot.j.bot_lots()["000001"][0] == 1


def test_watch_stop_loss_market_order_ignores_cap():
    # 장중 감시: AI 없이 손절 — 시장가, 하루 주문 한도를 다 써도 판다
    _, br, ai, bot = setup(holdings={"000001": Holding("000001", "종목1", 2, 20_000, 18_000)},
                           prices={"000001": 18_000, "000002": 30_000, "000003": 80_000}, max_orders_per_day=0)
    bot.j.open_position("000001", "종목1", "kr", 2, 20_000)        # -10% → 손절
    prices = []
    orig = br.sell
    br.sell = lambda code, qty, price=None: (prices.append(price), orig(code, qty, price))[1]
    bot.cfg.order_type = "limit"
    out, acted = bot.watch()
    assert ("SELL", "000001", 2) in br.orders and prices == [None] and acted, out
    assert ai.ctx is None                                             # AI 는 부르지 않음


def test_watch_quiet_when_closed_or_ok():
    _, br, _, bot = setup(holdings={"000001": Holding("000001", "종목1", 2, 20_000, 20_000)})
    bot.j.open_position("000001", "종목1", "kr", 2, 20_000)
    out, acted = bot.watch()
    assert br.orders == [] and not acted
    _, br2, _, bot2 = setup(holdings={"000001": Holding("000001", "종목1", 2, 20_000, 10_000)}, status="2")
    bot2.j.open_position("000001", "종목1", "kr", 2, 20_000)
    out, acted = bot2.watch()                                         # 장 마감이면 손절선이어도 조용히
    assert br2.orders == [] and not acted


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
