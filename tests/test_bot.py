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
                symbols=["000001", "000002", "000003"], mode=mode, data_dir=tmp)
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


def setup(mode="mock", decisions=(), prices=None, holdings=None, **kw):
    tmp = Path(tempfile.mkdtemp())
    cfg = make_cfg(tmp, mode, **kw)
    broker = FakeBroker(prices or {"000001": 20_000, "000002": 30_000, "000003": 80_000},
                        holdings)
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
    assert bot.bot_cash == 100_000 - 40_000 - 30_000, out


def test_low_confidence_and_dry_mode():
    _, br, _, bot = setup(decisions=[
        {"code": "000001", "action": "BUY", "amount_krw": 40_000, "confidence": 0.4, "reason": "r"}])
    bot.run()
    assert br.orders == []
    _, br, _, bot = setup(mode="dry", decisions=[
        {"code": "000001", "action": "BUY", "amount_krw": 40_000, "confidence": 0.9, "reason": "r"}])
    out = bot.run()
    assert ("BUY", "000001", 2) in br.orders  # FakeBroker는 그대로 받지만 장부는 안 바뀜
    assert bot.bot_cash == 100_000 and bot.j.bot_codes() == {}, out


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
    bot.j.set("bot_cash_mock", "70000")  # 원금 대비 -30%
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


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
