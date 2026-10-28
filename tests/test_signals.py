"""규칙 신호·하이브리드 모드 오프라인 테스트."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_bot import FakeAI, FakeBroker, make_cfg  # noqa: E402
from trader import indicators, signals  # noqa: E402
from trader.bot import Bot  # noqa: E402
from trader.broker import Holding  # noqa: E402
from trader.journal import Journal  # noqa: E402


def bars(start, up, down):
    c, out = start, []
    for i in range(80):
        c += up if i % 2 == 0 else -down
        out.append({"date": f"2026{i:04d}", "open": c, "high": c, "low": c, "close": c, "volume": 1000})
    return out


UP, DOWN, FLAT = bars(20000, 200, 100), bars(30000, 100, 200), bars(20000, 100, 100)


def cand(series, held=False, **kw):
    return {"code": "000001", "held": held, "warnings": [], "affordable": True, "today_change_pct": 0.5,
            **indicators.compute(series), **kw}


def test_buy_and_sell_signals():
    assert signals.buy_signal(cand(UP))["type"] == "BUY_TREND"
    assert signals.buy_signal(cand(DOWN)) is None and signals.buy_signal(cand(FLAT)) is None
    assert signals.buy_signal(cand(UP, warnings=["단기과열"])) is None          # 경고 종목
    assert signals.buy_signal(cand(UP, affordable=False)) is None
    assert signals.sell_signal(cand(DOWN, held=True))["type"] == "SELL_TREND_BREAK"
    assert signals.sell_signal(cand(FLAT, held=True)) is None                    # 횡보는 매도 아님
    assert signals.sell_signal(cand(UP, held=True)) is None
    assert signals.evaluate([cand(UP)], can_buy=False) == {}                     # 자리 없으면 매수 신호 무시


class SeriesBroker(FakeBroker):
    def __init__(self, series, prices, holdings=None):
        super().__init__(prices, holdings)
        self.series = series

    def daily(self, code, n=80):
        return self.series[code]


class CountingAI(FakeAI):
    calls = 0

    def decide(self, ctx):
        CountingAI.calls += 1
        return super().decide(ctx)


def make(series, decisions=(), holdings=None, **kw):
    cfg = make_cfg(Path(tempfile.mkdtemp()), ai_hybrid=True, **kw)
    prices = {c: s[-1]["close"] for c, s in series.items()}
    br = SeriesBroker(series, prices, holdings)
    ai = CountingAI(list(decisions))
    return cfg, br, ai, Bot(cfg, br, ai, Journal(cfg.db_path))


def test_hybrid_skips_ai_when_no_signal():
    CountingAI.calls = 0
    cfg, br, ai, bot = make({"000001": FLAT, "000002": DOWN, "000003": FLAT})
    out = bot.run()
    assert CountingAI.calls == 0 and "AI 호출 생략" in out and br.orders == []


def test_hybrid_sends_only_signaled_and_filters_others():
    CountingAI.calls = 0
    cfg, br, ai, bot = make({"000001": UP, "000002": FLAT, "000003": DOWN}, decisions=[
        {"code": "000001", "action": "BUY", "amount_krw": 30000, "confidence": 0.8, "reason": "확인"},
        {"code": "000002", "action": "BUY", "amount_krw": 30000, "confidence": 0.9, "reason": "신호 없는데 매수"},
    ])
    out = bot.run()
    assert CountingAI.calls == 1
    assert [c["code"] for c in ai.ctx["candidates"]] == ["000001"]
    assert "rule_signal" in ai.ctx["candidates"][0]
    assert [o[1] for o in br.orders] == ["000001"] and "규칙 신호 없는 종목" in out


def test_hybrid_held_sell_signal_reaches_ai():
    CountingAI.calls = 0
    h = {"000003": Holding("000003", "종목3", 1, 29000, 28900)}
    cfg, br, ai, bot = make({"000001": FLAT, "000002": FLAT, "000003": DOWN}, holdings=h, decisions=[
        {"code": "000003", "action": "SELL", "sell_ratio": 1, "confidence": 0.8, "reason": "이탈 확인"}],
        min_hold_days=0)
    bot.j.open_position("000003", "종목3")
    bot.run()
    assert [c["code"] for c in ai.ctx["candidates"]] == ["000003"]
    assert ("SELL", "000003", 1) in br.orders


def test_rules_only_provider_trades_without_ai():
    cfg = make_cfg(Path(tempfile.mkdtemp()), ai_hybrid=True)
    series = {"000001": UP, "000002": FLAT, "000003": DOWN}
    br = SeriesBroker(series, {c: s[-1]["close"] for c, s in series.items()})
    out = Bot(cfg, br, signals.RulesAnalyst(), Journal(cfg.db_path)).run()
    assert [o[:2] for o in br.orders] == [("BUY", "000001")], out
    assert "규칙만" in out


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
