"""재현 점검 — 백테스트 ↔ 그림자 장부."""
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from trader import replay, shadow  # noqa: E402
from trader.journal import Journal  # noqa: E402
import test_bot as tb  # noqa: E402


def test_match_window_and_rates():
    bt = [{"code": "A", "name": "가", "day": "20261112", "price": 100.0},     # 실시간 11-11 신호 → 다음 날 시가
          {"code": "B", "name": "나", "day": "20261113", "price": 50.0},       # 백테스트에만
          {"code": "A", "name": "가", "day": "20261201", "price": 90.0}]       # 날짜가 멀어 다른 매수
    sh = [{"code": "A", "name": "가", "day": "2026-11-11", "price": 101.0},
          {"code": "C", "name": "다", "day": "2026-11-12", "price": 10.0}]     # 실시간에만
    m = replay.match(bt, sh)
    assert len(m["pairs"]) == 1 and m["pairs"][0]["px_diff_pct"] == 1.0
    assert [x["code"] for x in m["only_bt"]] == ["B", "A"] and [x["code"] for x in m["only_shadow"]] == ["C"]
    assert m["match_rate"] == 25.0                                           # 1 / (1 + 1 + 2)
    assert replay.match([], [])["match_rate"] is None


def test_run_needs_shadow_then_compares():
    cfg = tb.make_cfg(Path(tempfile.mkdtemp()))
    cfg.strategy = {"signal": "breakout", "late_buy": False}
    try:
        replay.run(cfg, None)
        raise AssertionError("그림자 없으면 실패해야 함")
    except RuntimeError as e:
        assert "그림자" in str(e)
    j = Journal(cfg.db_path)
    j.set(f"shadow_start_{cfg.ledger}", "2026-11-02")
    shadow._ensure(j)
    j.db.execute("INSERT INTO shadow_trades(ledger,code,name,opened,ts,qty,buy,sell,pnl,pnl_pct,reason) "
                 "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (cfg.ledger, "000001", "가", "2026-11-03", "2026-11-06T10:00",
                                                    2, 10_000, 10_500, 1_000, 5.0, "익절"))
    j.db.commit()
    fake = {"trades": [{"code": "000001", "name": "가", "buy_date": "20261104", "buy": 9_900.0}],
            "open_list": [{"code": "000002", "name": "나", "day": "20261105", "avg": 5_000.0}],
            "return_pct": 0.5, "end": "20261113"}
    with mock.patch("trader.backtest.prepare", return_value=({}, {}, None, None)), \
            mock.patch("trader.backtest.run", return_value=fake):
        r = replay.run(cfg, None)
    assert r["bt_n"] == 2 and r["shadow_n"] == 1 and len(r["pairs"]) == 1 and r["match_rate"] == 50.0
    assert r["shadow_pct"] == 1.0 and r["gap"] == 0.5 and r["ok"] is None and "10건" in r["verdict"]
    assert replay.load(cfg.data_dir, cfg.market)["match_rate"] == 50.0


def test_backtest_page_card_and_button():
    os.environ.update(TRADER_DATA_DIR=tempfile.mkdtemp(), WEB_PASSWORD="secret-pass", TRADING_MODE="dry")
    from web import app as W
    c = W.cfg()
    replay.save(c.data_dir, c.market, {"ts": "2026-11-14T20:00:00", "start": "2026-11-02", "end": "20261113",
                                       "bt_return_pct": 0.5, "bt_n": 2, "shadow_pct": 1.0, "shadow_n": 1, "gap": 0.5,
                                       "ok": None, "verdict": "표본 부족", "pairs": [], "only_bt": [], "only_shadow": [],
                                       "match_rate": 50.0, "px_diff_avg": None, "criteria": "x"})
    cl = W.app.test_client()
    with cl.session_transaction() as s:
        s["ok"] = True
    h = cl.get("/backtest").get_data(as_text=True)
    assert "재현 점검" in h and "50.0%" in h and "최근 5년" in h
    started = []
    with mock.patch.object(W, "_start_job", lambda k, m, p: started.append((k, p)) or None):
        cl.post("/backtest", data={"mode": "replay"})
    assert started == [("backtest", {"replay": True})]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
