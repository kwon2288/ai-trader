"""백테스트: 장세(상승·횡보·하락)별 성과 · 위험조정 지표 · 슬리피지."""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from trader import backtest  # noqa: E402
from test_guard import _bars, _cfg  # noqa: E402


def _dates(n):
    d, out = date(2021, 1, 4), []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.strftime("%Y%m%d"))
        d += timedelta(days=1)
    return out


def _bench(closes):
    return [{"date": d, "open": c, "high": c, "low": c, "close": c, "volume": 1} for d, c in zip(_dates(len(closes)), closes)]


def test_regime3():
    up = [100 + i * 0.5 for i in range(300)]                       # 꾸준히 오름
    r = backtest._regime3(_bench(up))
    assert r[_dates(300)[-1]] == "up" and _dates(300)[198] not in r     # 200일 전엔 판정 안 함
    down = up + [up[-1] * (1 - 0.01 * i) for i in range(1, 80)]    # 급락 → 200일선 아래
    r = backtest._regime3(_bench(down))
    assert r[_dates(len(down))[-1]] == "down"
    flat = [100.0] * 300
    assert backtest._regime3(_bench(flat))[_dates(300)[-1]] == "side"


def test_metrics_pf_streak_sharpe_and_regimes():
    trades = [{"pnl": 100, "sell_date": "1"}, {"pnl": -50, "sell_date": "2"}, {"pnl": -30, "sell_date": "3"},
              {"pnl": -20, "sell_date": "4"}, {"pnl": 200, "sell_date": "5"}]
    ds = _dates(260)
    curve = [{"date": d, "equity": 1000 * (1.001 ** i)} for i, d in enumerate(ds)]
    bench = _bench([100.0] * 200 + [100 - i for i in range(60)])          # 마지막 60일은 하락장
    m = backtest.metrics(curve, trades, 1000, bench)
    assert m["profit_factor"] == 3.0 and m["max_losing_streak"] == 3
    assert m["sortino"] is None and m["sharpe"] is None or m["sharpe"] > 5           # 매일 같은 수익 → 흔들림 0
    assert m["cagr"] > 25
    dn = m["regimes"]["down"]
    assert dn["days"] == 59 and dn["bench"] < -50 and dn["ret"] > 5 and dn["mdd"] == 0
    assert backtest.metrics([], [], 1000)["profit_factor"] is None


def test_run_has_metrics_and_slippage_costs_money():
    flat = [(p, p, p, p, 1000) for p in [10_000, 9_950] * 40]
    rows = flat + [(10_000, 10_650, 10_000, 10_600, 3000)] + [(10_600, 10_700, 10_500, 10_650, 1000)] * 30
    data = {"000001": _bars(rows)}
    kw = dict(stop_pct=-50, take_pct=0.2, min_hold=0, signal_mode="breakout")
    a = backtest.run(_cfg(), data, {}, **kw)
    b = backtest.run(_cfg(), data, {}, slip_pct=0.5, **kw)
    assert a["n"] == b["n"] >= 1 and b["pnl"] < a["pnl"] and b["slip_pct"] == 0.5
    assert b["trades"][0]["buy"] > a["trades"][0]["buy"]                              # 살 때 비싸게
    assert "profit_factor" in a and "max_losing_streak" in a


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
