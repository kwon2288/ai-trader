"""단기 매매 — 신호 4가지, 최대 보유일(시간 청산), 단기 매매 비교, 적용."""
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from trader import backtest, indicators, signals, strategy  # noqa: E402
from trader.broker import Holding  # noqa: E402
import test_bot as tb  # noqa: E402
from test_strategy import bars  # noqa: E402


def test_signal_modes():
    base = {"code": "A", "held": False, "warnings": [], "affordable": True}
    dip = {**base, "close": 100, "ma60": 98, "rsi14": 30, "today_change_pct": 1.2}
    assert signals.evaluate([dip], mode="dip")["A"]["type"] == "BUY_DIP"
    assert "A" not in signals.evaluate([{**dip, "close": 90}], mode="dip")          # 60일선 한참 아래 → 안 삼
    assert signals.evaluate([{**base, "held": True, "rsi14": 62}], mode="dip")["A"]["type"] == "SELL_REBOUND"
    bo = {**base, "close": 105, "high_20d_prev": 100, "volume_vs_20d_avg": 2.0, "rsi14": 65}
    assert signals.evaluate([bo], mode="breakout")["A"]["type"] == "BUY_BREAKOUT"
    assert "A" not in signals.evaluate([{**bo, "volume_vs_20d_avg": 1.1}], mode="breakout")
    assert signals.evaluate([{**base, "held": True, "close": 95, "low_10d_prev": 96}], mode="breakout")["A"]["type"] == "SELL_CHANNEL"
    vo = {**base, "close": 105, "ma20": 100, "volume_vs_20d_avg": 3.0, "today_change_pct": 4}
    assert signals.evaluate([vo], mode="volume")["A"]["type"] == "BUY_VOLUME"
    assert "A" not in signals.evaluate([{**vo, "today_change_pct": 15}], mode="volume")     # 급등한 날은 거름
    assert signals.evaluate([{**base, "held": True, "close": 97, "ma5": 100}], mode="volume")["A"]["type"] == "SELL_MA5"
    assert signals.label("nope") == "추세 추종"


def test_indicators_have_breakout_fields():
    b = bars([100 + i for i in range(30)])
    ind = indicators.compute(b)
    assert ind["high_20d_prev"] == round(max(x["high"] for x in b[-21:-1]), 4)
    assert ind["low_10d_prev"] == round(min(x["low"] for x in b[-11:-1]), 4) and ind["ma10"]


def test_settings_defaults_and_validation():
    class C:
        market = "kr"
        strategy = {"signal": "weird", "max_hold_days": 999}
    s = strategy.settings(C)
    assert s["signal"] == "trend" and s["max_hold_days"] == 60


def test_backtest_max_hold_closes_positions():
    cfg = tb.make_cfg(Path(tempfile.mkdtemp()), total_krw=1_000_000, max_per_position_krw=300_000, max_positions=1)
    cfg.costs = {"kr_fee_pct": 0, "kr_sell_tax_pct": 0}
    import math
    data = {"000001": bars([round(10_000 * (1.002 ** i) * (1 + 0.02 * math.sin(i * 0.9))) for i in range(200)])}  # 오름 → 매수 후 계속 보유
    r0 = backtest.run(cfg, data, {}, stop_pct=-50, take_pct=200, min_hold=0)
    r5 = backtest.run(cfg, data, {}, stop_pct=-50, take_pct=200, min_hold=0, max_hold=5)
    assert r5["n"] > r0["n"] and any(t["why"] == "보유기간 만료" for t in r5["trades"])
    from datetime import datetime as D
    assert all((D.strptime(t["sell_date"], "%Y%m%d") - D.strptime(t["buy_date"], "%Y%m%d")).days <= 9
               for t in r5["trades"])                                                  # 5일 + 주말·다음 날 시가
    assert r5["signal"] == "trend" and r5["max_hold"] == 5 and r5["avg_trade_pct"] is not None


def test_short_sweep():
    cfg = tb.make_cfg(Path(tempfile.mkdtemp()), total_krw=1_000_000, max_per_position_krw=300_000, max_positions=2)
    import math
    data = {"000001": bars([round(10_000 * (1 + 0.08 * math.sin(i * 0.3)) * (1.001 ** i)) for i in range(400)])}

    class B:
        def history(self, code, days):
            return data["000001"][-days:]

        def quote(self, code):
            return type("Q", (), {"name": code})()
    sw = backtest.sweep(cfg, B(), 250, ["000001"], "069500", kind="short",
                        grid={"stop": [-5], "take": [7], "max_hold": [0, 5], "market_filter": [False]})
    assert sw["kind"] == "short" and len(sw["rows"]) == 8                           # 신호 4 × 최대 보유 2
    assert {x["signal"] for x in sw["rows"]} == set(signals.MODES)
    assert len(sw["best_by_signal"]) == 4 and all(x["core_pct"] == 0 for x in sw["rows"])
    assert backtest.grid_size(backtest.SHORT_GRID) == 288


def test_bot_max_hold_exit_and_rules_mode():
    _, br, ai, bot = tb.setup(holdings={"000001": Holding("000001", "종목1", 2, 20_000, 20_200)},
                              strategy={"max_hold_days": 3, "signal": "dip"})
    bot.j.open_position("000001", "종목1", "kr", 2, 20_000)
    bot.j.db.execute("UPDATE bot_positions SET opened='2026-01-01'")
    out = bot.run()
    assert ("SELL", "000001", 2) in br.orders and "보유기간 만료" in out, out
    assert ai.ctx is None or ai.ctx["rules"]["signal_mode"] == "dip"


def test_apply_writes_signal_and_max_hold():
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
        cl.post("/strategy/apply", data={"stop": "-5", "take": "7", "min_hold": "1", "core_pct": "0",
                                         "market_filter": "0", "signal": "breakout", "max_hold": "5"})
        t = tomllib.loads(p.read_text())
        assert t["strategy"]["signal"] == "breakout" and t["strategy"]["max_hold_days"] == 5
        assert t["risk"]["take_profit_pct"] == 7
        cl.post("/strategy/apply", data={"stop": "-5", "take": "7", "min_hold": "1", "core_pct": "0", "signal": "x"})
        assert tomllib.loads(p.read_text())["strategy"]["signal"] == "breakout"     # 모르는 신호는 거부
        cl.post("/strategy/save", data={"signal": "volume", "max_hold_days": "10", "core_pct": "0"})
        t = tomllib.loads(p.read_text())
        assert t["strategy"]["signal"] == "volume" and t["strategy"]["max_hold_days"] == 10
        h = cl.get("/backtest").get_data(as_text=True)
        assert "단기 매매 비교" in h and "눌림목 반등" in h
    finally:
        W.CONFIG_PATH = old


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
