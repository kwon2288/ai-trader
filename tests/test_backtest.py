"""백테스트 엔진 — 합성 데이터로 손절·익절·신호 실행과 미래 데이터 미사용 확인."""
import math
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from trader import backtest  # noqa: E402
from test_bot import make_cfg  # noqa: E402


def bars(prices, start=20250101):
    out = []
    for i, p in enumerate(prices):
        d = str(start + i) if i < 28 else str(start + 100 * (i // 28) + i % 28)
        out.append({"date": f"{int(d):08d}", "open": p, "high": p * 1.01, "low": p * 0.99, "close": p, "volume": 1000})
    out.sort(key=lambda b: b["date"])
    return out


def test_stop_loss_on_gap_down_next_day_execution():
    # 신호는 70번째 날 종가 → 71번째 날 시가에 매수, 이후 -15% 갭 하락 → 손절 (시가 체결)
    cfg = make_cfg(Path(tempfile.mkdtemp()), total_krw=1_000_000, max_per_position_krw=500_000, max_positions=2,
                   stop_loss_pct=-7, take_profit_pct=50, min_hold_days=0)
    cfg.costs = {"kr_fee_pct": 0, "kr_sell_tax_pct": 0}
    prices = [10_000] * 100 + [8_500] * 10
    data = {"000001": bars(prices)}
    dates = [b["date"] for b in data["000001"]]
    orig = backtest.signals.evaluate
    backtest.signals.evaluate = lambda cands, can_buy=True, **_k: (
        {"000001": {"type": "BUY_TREND", "strength": 0.7}} if any(
            c["code"] == "000001" and not c["held"] and c["close"] == 10_000 for c in cands) and can_buy else {})
    try:
        r = backtest.run(cfg, data, {"000001": "테스트"})
    finally:
        backtest.signals.evaluate = orig
    t = r["trades"][-1]
    assert t["buy_date"] == dates[backtest.WARMUP + 1]            # 신호 다음 날 시가
    assert t["why"] == "손절" and t["sell"] == 8_500 and t["pnl"] < 0   # 갭이면 시가로
    assert r["mdd_pct"] < 0 and math.isfinite(r["return_pct"])


def test_take_profit_and_bench():
    cfg = make_cfg(Path(tempfile.mkdtemp()), total_krw=1_000_000, max_per_position_krw=500_000, max_positions=1,
                   stop_loss_pct=-30, take_profit_pct=10, min_hold_days=0)
    prices = [10_000 * (1.004 ** i) * (1 + 0.02 * math.sin(i * 1.3)) for i in range(200)]
    data = {"000001": bars([round(x) for x in prices])}
    r = backtest.run(cfg, data, {"000001": "KODEX 200"}, bench=data["000001"])
    assert any(t["why"] == "익절" for t in r["trades"]) and r["return_pct"] > 0
    assert r["bench_pct"] > 0 and r["bench_curve"]


def test_run_job_cache_and_page():
    import os
    os.environ.update(TRADER_DATA_DIR=tempfile.mkdtemp(), WEB_PASSWORD="secret-pass", TRADING_MODE="dry")
    from trader import config as cfgmod
    calls = []

    class B:
        def history(self, code, days):
            calls.append(code)
            return bars([round(10_000 * (1.003 ** i) * (1 + 0.03 * math.sin(i * 0.9 + len(code)))) for i in range(days)])

        def quote(self, code):
            return type("Q", (), {"name": "종목" + code[-1]})()

    cfg = cfgmod.load()
    r = backtest.run_job(cfg, B(), 250, -7, 15, ["000001", "000002"], "069500")
    assert r["days"] > 100 and "bench_pct" in r and backtest.load(cfg.data_dir, "kr")["params"]["days"] == 250
    backtest.run_job(cfg, B(), 250, -5, 10, ["000001", "000002"], "069500")
    assert calls.count("000001") == 1                                  # 같은 날은 캐시
    # 짧게 받아 둔 캐시(손익 화면의 지수 비교 등)는 긴 백테스트에 재사용하지 않는다
    backtest.load_bars(B(), "SPY", 40, cfg.data_dir / "bars" / "x")
    assert len(backtest.load_bars(B(), "SPY", 300, cfg.data_dir / "bars" / "x")) == 300
    assert "fees" in r and "gross_return_pct" in r and isinstance(r["by_code"], list)
    from web.app import app
    cl = app.test_client()
    with cl.session_transaction() as s:
        s["ok"] = True
    h = cl.get("/backtest").get_data(as_text=True)
    assert "봇 전략 수익률" in h and "KODEX 200" in h


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
