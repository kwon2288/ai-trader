"""지수 비교 데이터(bench) · 주간 리포트."""
import json
import os
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from trader import bench, notify, runner  # noqa: E402
from trader.journal import Journal, now  # noqa: E402
import test_bot as tb  # noqa: E402

OK = SimpleNamespace(ok=True, order_no="1", message="")


def _closes(start: date, vals):
    out, d = [], start
    for v in vals:
        while d.weekday() >= 5:
            d += timedelta(days=1)
        out.append([d.strftime("%Y%m%d"), v])
        d += timedelta(days=1)
    return out


def test_bench_change_and_line():
    j = Journal(Path(tempfile.mkdtemp()) / "j.db")
    assert bench.change(j, "kr", date(2026, 11, 9)) is None                        # 데이터 없음
    closes = _closes(date(2026, 11, 2), [100, 101, 102, 103, 104, 105, 106, 107, 108, 110])  # 11/02 ~ 11/13
    j.set("bench_kr", json.dumps({"day": "x", "code": "069500", "name": "KODEX 200 (코스피200)", "closes": closes}))
    c = bench.change(j, "kr", date(2026, 11, 9), date(2026, 11, 13))
    assert c["pct"] == round((110 / 104 - 1) * 100, 2) and c["base_date"] == "20261106" and c["end_date"] == "20261113"
    assert bench.change(j, "kr", date(2026, 11, 14), date(2026, 11, 15))["pct"] == 0.0     # 기간에 거래일 없음
    assert bench.change(j, "kr", date(2026, 10, 1)) is None                                # 데이터 밖
    s = bench.line(j, "kr", date(2026, 11, 9), date(2026, 11, 13), -0.3, lambda v: f"{v:+,.0f}원", 100_000)
    assert s.startswith("지수 비교: KODEX 200 +5.77%") and "지수가 6.07%p 앞섬" in s and "+5,770원" in s


def test_bench_refresh_once_a_day():
    cfg = tb.make_cfg(Path(tempfile.mkdtemp()))
    j = Journal(cfg.db_path)
    calls = []

    class B:
        def daily(self, code, n):
            calls.append(code)
            return [{"date": "20261113", "open": 1, "high": 1, "low": 1, "close": 100, "volume": 1}]
    assert bench.refresh(cfg, B(), j) and calls == ["069500"]
    assert not bench.refresh(cfg, B(), j)                                          # 오늘 이미 받음
    assert bench.load(j, "kr")["closes"] == [["20261113", 100.0]]

    class Bad:
        def daily(self, code, n):
            raise RuntimeError("x")
    cfg2 = tb.make_cfg(Path(tempfile.mkdtemp()))
    assert not bench.refresh(cfg2, Bad(), Journal(cfg2.db_path))                   # 실패해도 예외 없음


def test_bot_change_uses_equity_runs():
    j = Journal(Path(tempfile.mkdtemp()) / "j.db")
    for ts, eq in (("2026-11-06T15:00:00+09:00", 100_000), ("2026-11-10T15:00:00+09:00", 101_000),
                   ("2026-11-13T15:00:00+09:00", 98_000)):
        j.db.execute("INSERT INTO runs(ts,mode,equity) VALUES(?,?,?)", (ts, "mock", eq))
    j.db.commit()
    assert bench.bot_change(j, "mock", 100_000, date(2026, 11, 9), date(2026, 11, 13)) == -2.0
    assert bench.bot_change(j, "mock", 100_000, date(2026, 12, 1)) is None


def _week_cfg():
    cfg = tb.make_cfg(Path(tempfile.mkdtemp()), ai_hybrid=True)
    cfg.costs = {"kr_fee_pct": 0, "kr_sell_tax_pct": 0}
    return cfg


def test_weekly_text():
    cfg = _week_cfg()
    j = Journal(cfg.db_path)
    L = cfg.ledger
    for code, nm, buy, sell in (("000001", "가", 10_000, 10_500), ("000002", "나", 10_000, 9_200)):
        j.add_order(L, "BUY", code, nm, 2, buy, OK, "AI", "filled")
        j.add_order(L, "SELL", code, nm, 2, sell, OK, "규칙", "filled")
    today = now().date()
    mon = today - timedelta(days=today.weekday())
    txt = runner.weekly_text(cfg, mon, max(today, mon + timedelta(days=4)))      # 주말에 돌려도 오늘 주문이 들어가게
    assert txt.startswith(f"🗓 주간 리포트 ({mon:%m/%d}~") and "🇰🇷 국내 — 매수 2건 · 매도 2건" in txt
    assert "승률 50% (1승 1패)" in txt and "최고 가 +5.0%" in txt and "최저 나 -8.0%" in txt
    assert "-600원" in txt and "🇺🇸" not in txt


def test_weekly_summary_timing():
    cfg = _week_cfg()
    sent = []
    o = notify.enabled, notify.channels, notify.send
    notify.enabled = lambda c, k: True
    notify.channels = lambda d: ["telegram"]
    notify.send = lambda c, k, t, force=False: sent.append((k, t))
    kst = ZoneInfo("Asia/Seoul")
    try:
        j = Journal(cfg.db_path)
        fri = datetime(2026, 11, 13, 15, 45, tzinfo=kst)
        assert not runner.weekly_summary(cfg, fri)                                 # 그 주 실행 기록 없음
        j.set("weekly_sent", "")
        j.db.execute("INSERT INTO run_logs(ts, mode, kind, source, output) VALUES(?,?,?,?,?)",
                     ("2026-11-12T10:00:00+09:00", cfg.ledger, "trade", "timer", "x"))
        j.db.commit()
        assert not runner.weekly_summary(cfg, datetime(2026, 11, 12, 16, 0, tzinfo=kst))   # 목요일
        assert not runner.weekly_summary(cfg, fri.replace(hour=15, minute=20))             # 금요일 마감 전
        assert runner.weekly_summary(cfg, fri) and sent[-1][0] == "weekly"
        assert not runner.weekly_summary(cfg, fri.replace(hour=17))                        # 주 1회
        assert not runner.weekly_summary(cfg, datetime(2026, 11, 14, 9, 0, tzinfo=kst))    # 토요일에도 이미 보냄
        j.set("weekly_sent", "")
        assert runner.weekly_summary(cfg, datetime(2026, 11, 14, 9, 0, tzinfo=kst))        # 금요일 휴장 → 토요일
        n = len(sent)
        assert runner.weekly_summary(cfg, force=True) and len(sent) == n + 1              # 직접 보내기
        assert j.get("weekly_sent") == "2026-11-09"                                        # 정기 발송 기록은 그대로
    finally:
        notify.enabled, notify.channels, notify.send = o


def test_pnl_all_has_market_compare():
    os.environ.update(TRADER_DATA_DIR=tempfile.mkdtemp(), WEB_PASSWORD="secret-pass", TRADING_MODE="dry")
    from web import app as W
    c = W.cfg()
    j = Journal(c.db_path)
    j.db.execute("INSERT INTO runs(ts,mode,equity) VALUES(?,?,?)", (now().isoformat(timespec="seconds"), c.ledger, 100))
    j.db.commit()
    orig = W.cfg

    def cfg_us():
        x = orig()
        x.us = {"enabled": True, "total_usd": 120, "symbols": ["KO"]}
        return x
    W.cfg = cfg_us
    try:
        cl = W.app.test_client()
        with cl.session_transaction() as s:
            s["ok"] = True
        h = cl.get("/pnl?m=all&p=all").get_data(as_text=True)
        assert "시장과 비교" in h and "🇰🇷 국내 봇" in h
        h = cl.get("/settings").get_data(as_text=True)
        assert "주간 리포트" in h and "n_weekly" in h
    finally:
        W.cfg = orig


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
