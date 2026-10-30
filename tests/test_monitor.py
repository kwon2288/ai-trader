"""실행 누락 감시 — 지난 실행 시각에 기록이 없으면 한 번만 알림."""
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.update(TRADER_DATA_DIR=tempfile.mkdtemp(), TRADING_MODE="mock")
from trader import config as cfgmod, monitor, schedule  # noqa: E402
from trader.journal import Journal  # noqa: E402

KST = ZoneInfo("Asia/Seoul")


def test_missed_and_notified_once():
    cfg = cfgmod.load()
    j = Journal(cfg.db_path)
    raw = {"schedule": {"kr": ["10:00", "14:30"]}}
    now = datetime(2026, 11, 2, 15, 0, tzinfo=KST)               # 월요일 15:00
    j.db.execute("INSERT INTO run_logs VALUES(?,?,?,?,?)", ("2026-11-02T10:00:04+09:00", "mock", "trade", "timer", "ok"))
    j.db.commit()
    assert monitor.missed_slots(j, cfg, raw, "kr", now) == ["14:30"]
    assert monitor.missed_slots(j, cfg, raw, "kr", datetime(2026, 11, 2, 14, 40, tzinfo=KST)) == []   # 아직 유예 중
    assert monitor.missed_slots(j, cfg, raw, "kr", datetime(2026, 11, 1, 15, 0, tzinfo=KST)) == []    # 일요일
    d = Path(tempfile.mkdtemp())
    schedule.SYSTEMD = d
    sent = []
    assert monitor.check(j, cfg, raw, lambda c, k, m: sent.append(m), now) == []      # 타이머 미설치면 조용히
    (d / "ai-trader.timer").write_text("x")
    monitor.check(j, cfg, raw, lambda c, k, m: sent.append(m), now)
    monitor.check(j, cfg, raw, lambda c, k, m: sent.append(m), now)
    assert len(sent) == 1 and "14:30" in sent[0]


def test_ping_uses_env_url():
    cfg = cfgmod.load()
    calls = []
    import requests
    old = requests.get
    requests.get = lambda url, timeout=None: calls.append(url)
    try:
        monitor.ping(cfg)                                          # 주소 없으면 아무것도 안 함
        os.environ["HEALTHCHECK_URL"] = "https://hc.example/abc"
        monitor.ping(cfg)
        monitor.ping(cfg, ok=False)
    finally:
        requests.get = old
        os.environ.pop("HEALTHCHECK_URL")
    assert calls == ["https://hc.example/abc", "https://hc.example/abc/fail"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
