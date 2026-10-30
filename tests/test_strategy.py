"""전략 옵션 — 시장 필터 · 코어-위성 (실매매·백테스트), 자동 비교, 적용."""
import math
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from trader import backtest, strategy  # noqa: E402
from trader.broker import Holding  # noqa: E402
import test_bot as tb  # noqa: E402
from datetime import date, timedelta  # noqa: E402


def bars(prices, skip=0):
    """skip: 앞의 몇 개는 버림 (같은 달력에서 뒷부분만)."""
    d, out = date(2023, 1, 2), []
    for p in [None] * skip + list(prices):
        while d.weekday() >= 5:
            d += timedelta(days=1)
        if p is not None:
            out.append({"date": d.strftime("%Y%m%d"), "open": p, "high": p * 1.01, "low": p * 0.99, "close": p,
                        "volume": 1000})
        d += timedelta(days=1)
    return out


def test_regime_from_bars():
    up = [{"close": 100 + i} for i in range(220)]
    down = [{"close": 300 - i} for i in range(220)]
    assert strategy.regime_from_bars(up)[0] is True
    assert strategy.regime_from_bars(down)[0] is False
    assert strategy.regime_from_bars(up[:50])[0] is None


def test_live_market_filter_blocks_buys():
    _, br, _, bot = tb.setup(decisions=[
        {"code": "000001", "action": "BUY", "amount_krw": 40_000, "confidence": 0.9, "reason": "r"}],
        strategy={"market_filter": True})
    orig = strategy.regime
    strategy.regime = lambda broker, cfg: (False, "지수 < 200일선")
    try:
        out = bot.run()
    finally:
        strategy.regime = orig
    assert br.orders == [] and "시장 필터" in out


def test_live_core_buys_and_is_not_traded():
    # 예산 10만원, 코어 50% → 코어 ETF(000003, 8만원)… 5만원으론 못 사니 000002(3만원)를 코어로
    _, br, ai, bot = tb.setup(decisions=[
        {"code": "000002", "action": "SELL", "sell_ratio": 1, "confidence": 0.9, "reason": "r"}],
        strategy={"core_pct": 50, "core_kr": "000002"})
    out = bot.run()
    assert ("BUY", "000002", 1) in br.orders and "코어" in out, out
    assert all(c["code"] != "000002" for c in ai.ctx["candidates"])          # AI 후보에서 빠짐
    # 다음 실행: 코어가 손절선 아래여도 팔지 않는다
    br.holdings["000002"] = Holding("000002", "종목2", 1, 30_000, 20_000)
    br.prices["000002"] = 20_000
    br.orders.clear()
    bot.run()
    assert not any(o[0] == "SELL" and o[1] == "000002" for o in br.orders)
    out, _ = bot.watch()
    assert not any(o[0] == "SELL" and o[1] == "000002" for o in br.orders)


def test_backtest_core_and_filter_and_sweep():
    cfg = tb.make_cfg(Path(tempfile.mkdtemp()), total_krw=1_000_000, max_per_position_krw=300_000, max_positions=2)
    cfg.costs = {"kr_fee_pct": 0, "kr_sell_tax_pct": 0}
    n = 420
    idx = [10_000 * (1.002 ** i) * (1 + 0.03 * math.sin(i * 0.7)) for i in range(n)]
    data = {"000001": bars([round(10_000 * (1 + 0.05 * math.sin(i * 0.33))) for i in range(n)], skip=200)}
    bench = bars([round(10_000 * (1.002 ** (i - 200))) for i in range(200)] + [round(x) for x in idx])
    r0 = backtest.run(cfg, data, {}, bench=bench)
    r1 = backtest.run(cfg, data, {}, bench=bench, core_pct=70, core_code="069500", core_bars=bench)
    assert r1["core_pct"] == 70 and r1["core_pnl"] > 0
    assert r1["return_pct"] > r0["return_pct"]                 # 오르는 지수를 70% 들고 있으면 더 낫다
    down = bars([round(40_000 - 50 * i) for i in range(n + 200)])     # 내내 200일선 아래
    rf = backtest.run(cfg, data, {}, bench=down, market_filter=True)
    assert rf["n"] == 0                                          # 시장 필터 → 새 매수 없음

    class B:
        def history(self, code, days):
            return bench[-days:] if code == "069500" else data["000001"][-days:]   # noqa

        def quote(self, code):
            return type("Q", (), {"name": code})()
    os.environ["TRADER_DATA_DIR"] = str(cfg.data_dir)
    cfg.strategy = {"core_kr": "069500"}
    sw = backtest.sweep(cfg, B(), 250, ["000001"], "069500",
                        grid={"stop": [-5, -10], "take": [10], "min_hold": [2], "market_filter": [False],
                              "core_pct": [0, 70]})
    assert len(sw["rows"]) == 4 and sw["rows"][0]["score"] >= sw["rows"][-1]["score"]
    assert backtest.load_sweep(cfg.data_dir, "kr")["rows"]


def test_apply_route_writes_config():
    import shutil
    import tomllib
    os.environ.update(TRADER_DATA_DIR=tempfile.mkdtemp(), WEB_PASSWORD="secret-pass")
    import web.app as W
    p = Path(tempfile.mkdtemp()) / "config.toml"
    shutil.copy(Path(__file__).resolve().parent.parent / "config.example.toml", p)
    old = W.CONFIG_PATH
    W.CONFIG_PATH = p
    try:
        cl = W.app.test_client()
        with cl.session_transaction() as s:
            s["ok"] = True
        cl.post("/strategy/apply", data={"stop": "-8", "take": "20", "min_hold": "7", "core_pct": "70", "market_filter": "1"})
        t = tomllib.loads(p.read_text())
        assert t["risk"]["stop_loss_pct"] == -8 and t["risk"]["take_profit_pct"] == 20 and t["risk"]["min_hold_days"] == 7
        assert t["strategy"]["core_pct"] == 70 and t["strategy"]["market_filter"] is True
        cl.post("/strategy/apply", data={"stop": "5", "take": "20", "min_hold": "7", "core_pct": "70"})
        assert tomllib.loads(p.read_text())["risk"]["stop_loss_pct"] == -8       # 잘못된 값은 거부
    finally:
        W.CONFIG_PATH = old


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
