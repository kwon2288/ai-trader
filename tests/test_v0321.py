"""v0.32.1 — 시장 필터 fail-closed · 그림자 장부 한도 · 코어 미투자 경고 · NH 주문 찾기."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from trader import backtest, shadow, strategy  # noqa: E402
from trader.journal import Journal  # noqa: E402
import test_bot as tb  # noqa: E402
from test_shadow import _bo, _cfg  # noqa: E402
from test_strategy import bars  # noqa: E402


def test_live_filter_blocks_when_index_unknown():
    """지수 조회 실패 → 신규 매수 차단 (강제 실행도 마찬가지), 매도는 그대로."""
    _, br, _, bot = tb.setup(decisions=[
        {"code": "000001", "action": "BUY", "amount_krw": 40_000, "confidence": 0.9, "reason": "r"}],
        strategy={"market_filter": True})
    orig = strategy.regime
    strategy.regime = lambda broker, cfg: (None, "지수 조회 실패: timeout")
    try:
        out = bot.run(force=True)
    finally:
        strategy.regime = orig
    assert br.orders == [] and "지수 확인 실패" in out


def test_backtest_filter_fail_closed_and_warns():
    cfg = tb.make_cfg(Path(tempfile.mkdtemp()), total_krw=1_000_000, max_per_position_krw=300_000)
    cfg.costs = {"kr_fee_pct": 0, "kr_sell_tax_pct": 0}
    data = {"000001": bars([10_000 + (i % 7) * 50 for i in range(300)])}
    r_off = backtest.run(cfg, data, {}, bench=None, signal_mode="dip")
    r_on = backtest.run(cfg, data, {}, bench=None, market_filter=True, signal_mode="dip")    # 지수 없음
    assert r_on["n"] == 0 and r_on["open_positions"] == 0 and r_on["filter_missing_days"] > 200
    assert r_off["filter_missing_days"] == 0


def test_backtest_core_missing_warning():
    cfg = tb.make_cfg(Path(tempfile.mkdtemp()), total_krw=1_000_000, max_per_position_krw=300_000)
    data = {"000001": bars([10_000] * 120)}
    r = backtest.run(cfg, data, {}, core_pct=50, core_code="069500", core_bars=None)
    assert r["core_missing"] and r["core_target"] == 500_000 and r["core_invested"] == 0 and r["core_idle"] == 500_000
    ok = backtest.run(cfg, data, {}, core_pct=50, core_code="069500", core_bars=bars([10_000] * 120))
    assert not ok["core_missing"] and ok["core_invested"] > 0


def test_shadow_daily_order_cap():
    cfg = _cfg(max_orders_per_day=1, max_positions=5)
    j = Journal(cfg.db_path)
    cands = [_bo("000001", 10_000), _bo("000002", 10_000), _bo("000003", 10_000)]
    r = shadow.step(j, cfg, cands, {c["code"]: c["price"] for c in cands}, None, print)
    assert r["bought"] == 1                                                     # 하루 1건
    assert shadow.step(j, cfg, cands, {c["code"]: c["price"] for c in cands}, None, print)["bought"] == 0
    code = next(iter(shadow.positions(j, cfg.ledger)))
    assert shadow.watch(j, cfg, {code: 9_000}, print) == 1                      # 손절은 한도와 무관


def test_shadow_daily_loss_blocks_buys_but_allows_sells():
    cfg = _cfg(max_positions=3, daily_loss_pct=-1)
    j = Journal(cfg.db_path)
    shadow.step(j, cfg, [_bo("000001", 10_000)], {"000001": 10_000}, None, print)   # 기준점 + 매수 (5주·5만원)
    p = {"000001": 9_700}                                                        # −3% → 예산 대비 −1.5%
    r = shadow.step(j, cfg, [_bo("000002", 10_000), dict(_bo("000001", 9_700), held=True)],
                    {**p, "000002": 10_000}, None, print)
    assert r["bought"] == 0                                                      # 손실 한도 → 새 매수 없음
    assert shadow.watch(j, cfg, {"000001": 9_200}, print) == 1                  # 매도(손절)는 계속


def test_nh_find_orders_parses_rows():
    from trader.broker import NHBroker
    b = NHBroker.__new__(NHBroker)
    b.account = "1"
    rows = [{"iem_cd": "A005930", "sby_dit_cd_nm": "현금매수", "orr_qty": "2", "tot_cns_qty": "2", "ny_cns_qty": "0",
             "can_qty": "0", "cns_avg_uit_pr": "70100", "itg_orr_no": "0001234"},
            {"iem_cd": "A005930", "sby_dit_cd_nm": "현금매도", "orr_qty": "2", "tot_cns_qty": "2", "itg_orr_no": "9"},
            {"iem_cd": "A000660", "sby_dit_cd_nm": "현금매수", "orr_qty": "2", "tot_cns_qty": "2", "itg_orr_no": "8"},
            {"iem_cd": "A005930", "sby_dit_cd_nm": "현금매수", "orr_qty": "3", "tot_cns_qty": "0", "itg_orr_no": "7"}]
    b._post = lambda path, body: {"Output_0": rows}
    out = b.find_orders("005930", "BUY", 2, "2026-11-14")
    assert len(out) == 1 and out[0][0] == "1234" and out[0][1].filled == 2 and out[0][1].avg_price == 70_100
    assert out[0][1].state == "filled"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
