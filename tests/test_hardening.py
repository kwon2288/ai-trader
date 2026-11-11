"""로그인 차단 · AI 비용 뺀 순손익 · 앞/뒤 기간 검증 · 단계별 시간 · 하루 손실 한도 · 일봉 캐시 · 마감 전 정리."""
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from trader import backtest, barcache, bot as botmod, pricing  # noqa: E402
from trader.broker import Holding, Quote  # noqa: E402
from trader.journal import Journal  # noqa: E402
import test_bot as tb  # noqa: E402
from test_strategy import bars  # noqa: E402


def web():
    os.environ.update(TRADER_DATA_DIR=tempfile.mkdtemp(), WEB_PASSWORD="secret-pass")
    import web.app as W
    from web import loginguard
    loginguard.reset()
    return W


def test_login_lockout_per_ip_and_global():
    W = web()
    from web import loginguard
    W.time.sleep = lambda s: None
    cl = W.app.test_client()
    for _ in range(5):
        cl.post("/login", data={"password": "wrong"}, environ_base={"REMOTE_ADDR": "1.2.3.4"})
    r = cl.post("/login", data={"password": "secret-pass"}, environ_base={"REMOTE_ADDR": "1.2.3.4"})
    assert r.status_code == 200 and "막혔어요" in r.get_data(as_text=True)    # 맞는 비밀번호도 차단 중엔 안 됨
    r = cl.post("/login", data={"password": "secret-pass"}, environ_base={"REMOTE_ADDR": "5.6.7.8"})
    assert r.status_code == 302                                                 # 다른 곳은 됨
    logs = Journal(W.cfg().db_path).logs(5)
    assert any(l["kind"] == "security" for l in logs)
    # 프록시 뒤: 앞쪽 X-Forwarded-For 는 바꿔도 마지막(프록시가 붙인) 주소로 센다
    loginguard.reset()
    for i in range(5):
        cl.post("/login", data={"password": "x"}, headers={"X-Forwarded-For": f"9.9.9.{i}, 10.0.0.1"})
    assert loginguard.blocked("10.0.0.1")
    # 전체 잠금: IP 를 바꿔 가며 20번
    loginguard.reset()
    for i in range(20):
        loginguard.fail(f"2.2.2.{i}")
    assert loginguard.blocked("3.3.3.3")


def test_ai_cost_and_net_pnl():
    W = web()
    c = W.cfg()
    j = Journal(c.db_path)
    j.add_usage(c.ledger, "claude", "claude-haiku-5-5", 1_000_000, 0, "timer")     # $0.10
    j.add_usage("mock_us", "claude", "claude-haiku-5-5", 1_000_000, 0, "timer")    # 다른 시장 → 제외
    a = pricing.ai_cost(j, c.data_dir, c.ledger)
    assert a["calls"] == 1 and abs(a["usd"] - 0.10) < 1e-6
    cl = W.app.test_client()
    with cl.session_transaction() as s:
        s["ok"] = True
    h = cl.get("/pnl?p=all").get_data(as_text=True)
    assert "AI 비용 뺀 실현 손익" in h and "−140원" in h                           # $0.10 × 1,400


def test_sweep_has_out_of_sample():
    import math
    cfg = tb.make_cfg(Path(tempfile.mkdtemp()), total_krw=1_000_000, max_per_position_krw=300_000)
    data = {"000001": bars([round(10_000 * (1 + 0.08 * math.sin(i * 0.3)) * (1.001 ** i)) for i in range(400)])}

    class B:
        def history(self, code, days):
            return data["000001"][-days:]

        def quote(self, code):
            return type("Q", (), {"name": code})()
    sw = backtest.sweep(cfg, B(), 300, ["000001"], "000001", kind="short",
                        grid={"signal": ["trend", "dip"], "stop": [-5], "take": [7], "max_hold": [0], "market_filter": [False], "guard": [False]})
    assert sw["split"] and sw["validation"]["top"] == 2
    x = sw["rows"][0]
    assert "oos_return_pct" in x and x["oos_n"] >= 0
    # 기간을 나눈 두 결과는 서로 다른 날짜만 쓴다
    r1 = backtest.run(cfg, data, {}, date_to=sw["split"]["is_end"])
    r2 = backtest.run(cfg, data, {}, date_from=sw["split"]["oos_start"])
    assert r1["end"] <= sw["split"]["is_end"] < sw["split"]["oos_start"] <= r2["start"]


def test_stage_times_in_log():
    _, br, ai, bot = tb.setup(decisions=[])
    out = bot.run()
    assert "⏱ 단계별 시간" in out


def test_daily_loss_limit_blocks_buys():
    cfg, br, ai, bot = tb.setup(decisions=[
        {"code": "000001", "action": "BUY", "amount_krw": 40_000, "confidence": 0.9, "reason": "r"}],
        daily_loss_pct=-5)
    day = datetime.now(ZoneInfo("Asia/Seoul")).date().isoformat()
    bot.j.set(f"day_start_{cfg.ledger}", f"{day}|110000")          # 아침엔 11만원 → 지금 10만원 = 예산의 -10%
    out = bot.run()
    assert br.orders == [] and "하루 손실 한도" in out, out
    # 끄면(0) 산다
    cfg2, br2, _, bot2 = tb.setup(decisions=[
        {"code": "000001", "action": "BUY", "amount_krw": 40_000, "confidence": 0.9, "reason": "r"}])
    bot2.j.set(f"day_start_{cfg2.ledger}", f"{day}|110000")
    bot2.run()
    assert br2.orders


def test_bar_cache():
    d = tempfile.mkdtemp()
    calls = []

    class B:
        def daily(self, code, n):
            calls.append(code)
            return [{"date": f"2020{i:04d}", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1} for i in range(n)]

    def q(status="1", vol=500):
        return Quote("A", "A", 10, 0, 10, 10, status, "", "", 0, 0, 0, 0, 0, [], 9, 11, 8, vol)
    day = barcache.market_day("kr")
    st = {}
    b1 = barcache.daily(B(), "A", q(), d, "kr", 80, st)                 # 처음 → 받음
    b2 = barcache.daily(B(), "A", q(), d, "kr", 80, st)                 # 같은 날 → 캐시 + 오늘 봉
    assert len(calls) == 1 and st == {"miss": 1, "hit": 1}
    assert b2[-1]["date"] == day and b2[-1]["close"] == 10 and b2[-1]["volume"] == 500 and len(b2) == 80
    barcache.daily(B(), "A", q(status="0"), d, "kr", 80)                 # 장 시작 전 → 새로 받음
    barcache.daily(B(), "A", q(vol=0), d, "kr", 80)                      # 거래량 모름 → 새로 받음
    assert len(calls) == 3
    assert b1 != b2


def test_near_close():
    kst = ZoneInfo("Asia/Seoul")
    assert botmod.near_close("kr", datetime(2026, 11, 2, 15, 10, tzinfo=kst)) == 20
    assert botmod.near_close("kr", datetime(2026, 11, 2, 14, 30, tzinfo=kst)) == 0
    assert botmod.near_close("kr", datetime(2026, 11, 7, 15, 10, tzinfo=kst)) == 0      # 토요일
    et = ZoneInfo("America/New_York")
    assert botmod.near_close("us", datetime(2026, 11, 2, 15, 45, tzinfo=et)) == 15


def test_watch_cancels_before_close():
    cfg, br, _, bot = tb.setup(holdings={"000001": Holding("000001", "종목1", 1, 20_000, 20_100)})
    bot.j.open_position("000001", "종목1", "kr", 1, 20_000)
    seen = {}
    from trader import fills
    orig, orig_nc = fills.reconcile, botmod.near_close
    fills.reconcile = lambda j, c, b, say, cancel_before=None, notify=None: seen.setdefault("cut", cancel_before)
    botmod.near_close = lambda m, at=None: 10
    try:
        out, _ = bot.watch()
    finally:
        fills.reconcile, botmod.near_close = orig, orig_nc
    assert "장 마감 10분 전" in out
    cut = datetime.fromisoformat(seen["cut"])
    assert (datetime.now(cut.tzinfo) - cut).total_seconds() < 6 * 60       # 5분 넘은 주문은 모두 취소 대상


def test_close_summary():
    from trader import notify, runner
    d = tempfile.mkdtemp()
    cfg = tb.make_cfg(Path(d))
    sent = []
    o_en, o_ch, o_send, o_rb = notify.enabled, notify.channels, notify.send, runner.refresh_balance
    notify.enabled = lambda c, k: True
    notify.channels = lambda dd: ["telegram"]
    notify.send = lambda c, k, t, force=False: sent.append(t)
    runner.refresh_balance = lambda base, mk: {}
    kst = ZoneInfo("Asia/Seoul")
    try:
        j = Journal(cfg.db_path)
        assert not runner.close_summary(cfg, "kr", datetime(2026, 11, 3, 15, 45, tzinfo=kst))   # 오늘 실행 없음(휴장 등)
        j.add_log(cfg.ledger, "trade", "timer", "x")
        today = datetime.now(kst)
        assert not runner.close_summary(cfg, "kr", today.replace(hour=15, minute=20))           # 마감 전
        if today.weekday() < 5:
            assert runner.close_summary(cfg, "kr", today.replace(hour=15, minute=45))
            assert not runner.close_summary(cfg, "kr", today.replace(hour=16, minute=0))        # 하루 한 번
            assert "🇰🇷 국내 오늘 마감 요약" in sent[0]
    finally:
        notify.enabled, notify.channels, notify.send, runner.refresh_balance = o_en, o_ch, o_send, o_rb


def test_timer_state_and_enable_route():
    from trader import schedule
    import subprocess
    calls = []

    class R:
        def __init__(self, out=""):
            self.stdout = out
    state = {"load": "not-found"}

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if cmd[1] == "show":
            return R(f"LoadState={state['load']}\nActiveState={'active' if state['load'] == 'loaded' else 'inactive'}\n"
                     "NextElapseUSecRealtime=Tue 2026-11-03 14:30:00 KST")
        if cmd[1] == "enable":
            state["load"] = "loaded"
        return R()
    o_run, o_which, o_sys = subprocess.run, schedule.shutil.which, schedule.SYSTEMD
    schedule.subprocess.run = fake_run
    schedule.shutil.which = lambda x: "/bin/systemctl"
    schedule.SYSTEMD = Path(tempfile.mkdtemp())
    try:
        assert schedule.timer_state("kr")["state"] == "missing"
        W = web()
        cl = W.app.test_client()
        with cl.session_transaction() as s:
            s["ok"] = True
        h = cl.get("/").get_data(as_text=True)
        assert "자동 실행 꺼짐" in h and "자동 실행 켜기" in h
        cl.post("/schedule/enable", data={"market": "kr"})
        assert (schedule.SYSTEMD / "ai-trader.timer").exists() and (schedule.SYSTEMD / "ai-trader.service").exists()
        assert ["systemctl", "enable", "--now", "ai-trader.timer"] in calls
        assert schedule.timer_state("kr")["state"] == "active"
        assert "자동 실행 꺼짐" not in cl.get("/").get_data(as_text=True)
    finally:
        schedule.subprocess.run, schedule.shutil.which, schedule.SYSTEMD = o_run, o_which, o_sys


def test_pick_uses_out_of_sample():
    rows = [{"oos_return_pct": -2, "oos_bench_pct": 5}, {"oos_return_pct": 6, "oos_bench_pct": 8},
            {"oos_return_pct": 9, "oos_bench_pct": 8}]
    assert backtest.pick(rows) == 2                         # 뒤 기간 플러스 + 지수보다 나은 첫 줄
    rows[2]["oos_return_pct"] = 7
    assert backtest.pick(rows) == 1                         # 지수를 이긴 게 없으면 플러스인 첫 줄
    assert backtest.pick([{"oos_return_pct": -1}]) is None
    assert backtest.pick([{"return_pct": 3}]) == 0          # 검증 없는 예전 결과
    # 화면: 추천이 그 줄에 붙는다
    import json
    W = web()
    c = W.cfg()
    base = {"signal": "breakout", "stop": -3, "take": 15, "min_hold": 1, "max_hold": 10, "market_filter": False,
            "core_pct": 0, "return_pct": 23.8, "mdd_pct": -9.4, "n": 118, "win_rate": 50, "fees": 1, "bench_pct": 10,
            "avg_trade_pct": 0.9, "gross_return_pct": 25, "oos_mdd_pct": -11, "oos_n": 50, "oos_bench_pct": 20,
            "oos_avg_trade_pct": 0.1, "score": 1}
    sw = {"ts": "2026-11-05T08:00:00", "days": 750, "symbols": ["A"], "kind": "short", "bench_pct": 10,
          "rows": [{**base, "oos_return_pct": -2.21}, {**base, "stop": -8, "oos_return_pct": 6.42}],
          "seconds": 1, "start": "20240101", "end": "20261101", "fee_pct": 0.015, "best_by_signal": [], "positive": 2,
          "split": {"is_end": "20250901", "oos_start": "20250902"},
          "validation": {"top": 2, "oos_positive": 1, "oos_beat_bench": 0, "oos_bench_pct": 20, "oos_avg_top": 2}}
    (Path(c.data_dir) / "sweep_kr.json").write_text(json.dumps(sw))
    cl = W.app.test_client()
    with cl.session_transaction() as s:
        s["ok"] = True
    h = cl.get("/backtest?market=kr").get_data(as_text=True)
    assert "추천 = 2번" in h


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
