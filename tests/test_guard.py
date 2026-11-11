"""보호 규칙 — 추격 매수 차단 · 돌파 실패 정리 · 이익 보호 · 돌파 매수는 마지막 실행에서만 · AI 성적표."""
import os
import shutil
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from trader import backtest, scorecard, signals, strategy  # noqa: E402
from trader.bot import Bot, last_slot_wait  # noqa: E402
from trader.journal import KST, Journal  # noqa: E402
import test_bot as tb  # noqa: E402

G = {"chase_max_ret5": 12.0, "fail_exit": True, "protect_at": 5.0, "trail": 5.0}


def test_settings_and_floor():
    class C:
        market = "kr"
        strategy = {"chase_max_ret5": "abc", "kr": {"guard": False}}
    s = strategy.settings(C)
    assert s["guard"] is False and s["late_buy"] is True and s["chase_max_ret5"] == 12.0 and s["trail_pct"] == 5.0
    assert strategy.guard_opts(s) is None and strategy.guard_opts(s, True)["fail_exit"] is True
    assert strategy.protect_floor(100, 104, G) is None                       # 아직 +5% 를 못 넘음
    assert abs(strategy.protect_floor(100, 105, G) - 100.5) < 1e-9                      # 본전(+0.5%) > 고점 −5%(99.75)
    assert abs(strategy.protect_floor(100, 120, G) - 114) < 1e-9             # 고점 −5%
    assert abs(strategy.protect_floor(100, 120, {**G, "trail": 0}) - 100.5) < 1e-9      # 본전만
    assert strategy.protect_floor(100, 120, None) is None


def test_chase_block_and_failed_breakout():
    base = {"code": "A", "name": "가", "held": False, "warnings": [], "affordable": True}
    bo = {**base, "close": 105, "high_20d_prev": 100, "volume_vs_20d_avg": 2.0, "rsi14": 65, "ret_5d_pct": 17.9}
    assert signals.evaluate([bo], mode="breakout")["A"]["type"] == "BUY_BREAKOUT"          # 보호 규칙 없으면 그대로
    skipped = []
    assert signals.evaluate([bo], mode="breakout", opts=G, skipped=skipped) == {}           # 5일 +17.9% → 안 삼
    assert skipped and "17.9" in skipped[0][1]
    assert "A" in signals.evaluate([{**bo, "ret_5d_pct": 8.0}], mode="breakout", opts=G)
    assert "A" in signals.evaluate([bo], mode="breakout", opts={**G, "chase_max_ret5": 0})  # 0 = 끔
    held = {**base, "held": True, "close": 98.5, "breakout_level": 100, "low_10d_prev": 90}
    s = signals.evaluate([held], mode="breakout", opts=G)["A"]
    assert s["type"] == "SELL_FAILED_BREAKOUT" and s["urgent"]
    assert signals.evaluate([{**held, "close": 99.5}], mode="breakout", opts=G) == {}       # 1% 안쪽은 잡음
    assert signals.evaluate([held], mode="breakout") == {}                                  # 꺼져 있으면 안 팖
    assert signals.evaluate([{**held, "breakout_level": None}], mode="breakout", opts=G) == {}
    assert signals.evaluate([held], mode="trend", opts=G) == {} or "SELL_FAILED" not in str(
        signals.evaluate([held], mode="trend", opts=G))                                     # 돌파 전략에서만


def _bars(rows):
    d, out = date(2024, 1, 1), []
    for o, h, l, c, v in rows:
        while d.weekday() >= 5:
            d += timedelta(days=1)
        out.append({"date": d.strftime("%Y%m%d"), "open": o, "high": h, "low": l, "close": c, "volume": v})
        d += timedelta(days=1)
    return out


def _cfg():
    cfg = tb.make_cfg(Path(tempfile.mkdtemp()), total_krw=1_000_000, max_per_position_krw=300_000, max_positions=1)
    cfg.costs = {"kr_fee_pct": 0, "kr_sell_tax_pct": 0}
    return cfg


def test_backtest_failed_breakout_exits_early():
    flat = [(p, p, p, p, 1000) for p in [10_000, 9_950] * 40]                  # 20일 고점 10,000
    rows = flat + [(10_000, 10_650, 10_000, 10_600, 3000),                     # 돌파 (거래량 3배)
                   (10_500, 10_550, 10_350, 10_400, 1000),                     # 다음 날 시가 10,500 에 매수
                   (10_300, 10_300, 9_700, 9_700, 1000),                       # 돌파선 10,000 아래로 −3%
                   (9_650, 9_700, 9_600, 9_650, 1000)] + [(9_650, 9_700, 9_600, 9_650, 1000)] * 12
    data = {"000001": _bars(rows)}
    kw = dict(stop_pct=-50, take_pct=200, min_hold=7, signal_mode="breakout")
    off = backtest.run(_cfg(), data, {}, **kw)
    on = backtest.run(_cfg(), data, {}, guard=G, **kw)
    assert on["n"] == 1 and on["trades"][0]["why"] == "돌파 실패" and on["guard"] and on["by_why"] == {"돌파 실패": 1}
    assert on["trades"][0]["sell_date"] < off["trades"][0]["sell_date"] if off["n"] else True   # 최소 보유일 안 기다림
    assert not off["guard"] and all(t["why"] != "돌파 실패" for t in off["trades"])


def test_backtest_profit_protection():
    flat = [(p, p, p, p, 1000) for p in [10_000, 9_950] * 40]
    up = [(10_000, 10_650, 10_000, 10_600, 3000), (10_500, 10_700, 10_450, 10_700, 1500),      # 시가 10,500 매수
          (10_800, 11_400, 10_800, 11_300, 1500), (11_300, 11_500, 11_200, 11_400, 1200)]      # 고점 11,500 (+9.5%)
    down = [(11_300, 11_350, 10_800, 10_850, 1200), (10_800, 10_800, 10_300, 10_300, 1200)] + \
           [(10_300, 10_350, 10_250, 10_300, 1000)] * 10
    data = {"000001": _bars(flat + up + down)}
    kw = dict(stop_pct=-50, take_pct=200, min_hold=30, signal_mode="breakout")
    on = backtest.run(_cfg(), data, {}, guard={**G, "fail_exit": False}, **kw)
    t = on["trades"][0]
    assert t["why"] == "이익 보호" and abs(t["sell"] - 11_500 * 0.95) < 1 and t["pnl"] > 0      # 고점 −5% = 10,925
    off = backtest.run(_cfg(), data, {}, **kw)
    assert all(x["why"] != "이익 보호" for x in off["trades"])


def test_bot_protect_and_urgent_sell():
    cfg = tb.make_cfg(Path(tempfile.mkdtemp()))
    b = Bot.__new__(Bot)
    b.cfg, b.j, b.lines = cfg, Journal(cfg.db_path), []
    b.say = b.lines.append
    b.j.open_position("000001", "종목1", "kr", 2, 10_000)
    h = SimpleNamespace(name="종목1", avg_price=10_000, now_price=10_600, pnl_pct=6.0)
    assert b._exit_trigger(h, "000001") is None                                 # 오르는 중 — 고점만 기록
    assert b.j.get(f"peak_{cfg.ledger}_000001").endswith("|10600.0")
    h.now_price, h.pnl_pct = 10_300, 3.0
    assert b._exit_trigger(h, "000001") is None                                 # 보호선 10,070 위
    h.now_price, h.pnl_pct = 10_050, 0.5
    assert b._exit_trigger(h, "000001") == "이익 보호" and "보호선" in b.lines[-1]
    cfg.strategy = {"guard": False}
    assert b._exit_trigger(h, "000001") is None                                 # 꺼 두면 안 팖
    cfg.strategy = {}
    b.j.close_position("000001")                                                # 다시 사면 고점은 처음부터
    b.j.open_position("000001", "종목1", "kr", 2, 10_000)
    b.j.db.execute("UPDATE bot_positions SET opened='2026-01-05'")
    b.j.db.commit()
    assert b._exit_trigger(h, "000001") is None and b.j.get(f"peak_{cfg.ledger}_000001") == "2026-01-05|10050.0"


def test_last_slot_wait():
    cfg = tb.make_cfg(Path(tempfile.mkdtemp()))
    tz = ZoneInfo("Asia/Seoul")
    last = last_slot_wait(cfg, datetime(2026, 11, 11, 10, 1, tzinfo=tz))
    assert last and last >= "14:00"                                             # 오전 실행 → 마지막 실행 시각까지 대기
    assert last_slot_wait(cfg, datetime(2026, 11, 11, 15, 25, tzinfo=tz)) is None
    hh, mm = int(last[:2]), int(last[3:])
    assert last_slot_wait(cfg, datetime(2026, 11, 11, hh, mm, tzinfo=tz)) is None   # 마지막 실행 자체는 매수 가능


def test_scorecard():
    cfg = tb.make_cfg(Path(tempfile.mkdtemp()))
    j = Journal(cfg.db_path)
    scorecard.record(j, cfg.ledger, "kr", "000001", "산것", 10_000, True, "돌파")
    scorecard.record(j, cfg.ledger, "kr", "000002", "막은것", 20_000, False, "돌파")
    scorecard.record(j, cfg.ledger, "kr", "000002", "막은것", 20_000, False, "같은 날 다시")     # 덮어씀
    assert j.db.execute("SELECT COUNT(*) n FROM signal_log").fetchone()["n"] == 2
    s = scorecard.summary(j, cfg.ledger)
    assert s["pending"] == 2 and s["taken"]["n"] == 0 and "표본" in s["verdict"]
    old = (datetime.now(KST).date() - timedelta(days=10)).isoformat()
    j.db.execute("UPDATE signal_log SET day=?", (old,))
    j.db.commit()
    key = old.replace("-", "")

    class B:
        def daily(self, code, n=60):
            px = {"000001": 10_000, "000002": 20_000}[code]
            d0 = datetime.fromisoformat(old)
            return [{"date": (d0 + timedelta(days=i)).strftime("%Y%m%d"), "close": px * (1 + 0.01 * i)} for i in range(-3, 8)]
    said = []
    assert scorecard.update(j, cfg, B(), said.append) == 2 and "성적표" in said[0]
    s = scorecard.summary(j, cfg.ledger)
    assert s["taken"] == {"n": 1, "avg": 5.0, "wins": 1} and s["blocked"]["avg"] == 5.0 and s["pending"] == 0, (s, key)
    assert scorecard.update(j, cfg, B()) == 0


def test_web_pages_and_save():
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
        h = cl.get("/settings").get_data(as_text=True)
        assert "보호 규칙" in h and "chase_max_ret5" in h and "돌파 매수는 그날 마지막 실행에서만" in h
        form = {"kr_signal": "breakout", "kr_max_hold_days": "0", "kr_stop": "-5", "kr_take": "10", "kr_min_hold": "2",
                "kr_core_pct": "0", "kr_guard": "on", "chase_max_ret5": "10", "protect_at_pct": "4", "trail_pct": "3"}
        cl.post("/strategy/save", data=form)
        t = tomllib.loads(p.read_text())["strategy"]
        assert t["kr"]["guard"] is True and t["kr"]["late_buy"] is False and t["chase_max_ret5"] == 10 and t["trail_pct"] == 3
        cl.post("/strategy/save", data={**form, "chase_max_ret5": "500"})                  # 범위 밖 → 거부
        assert tomllib.loads(p.read_text())["strategy"]["chase_max_ret5"] == 10
        cl.post("/strategy/apply", data={"stop": "-5", "take": "7", "min_hold": "1", "core_pct": "0",
                                         "market_filter": "0", "signal": "breakout", "max_hold": "5", "guard": "0"})
        assert tomllib.loads(p.read_text())["strategy"]["kr"]["guard"] is False
        assert "보호 규칙" in cl.get("/backtest").get_data(as_text=True)
        c = W.cfg()
        scorecard.record(Journal(c.db_path), c.ledger, "kr", "000001", "성적표종목", 10_000, True, "돌파")
        h = cl.get("/pnl?m=kr").get_data(as_text=True)
        assert ("AI 성적표" in h and "성적표종목" in h) or not c.ai_hybrid
    finally:
        W.CONFIG_PATH = old


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
