"""자동 실행 시각 — 입력 검증과 systemd drop-in 작성."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from trader import schedule  # noqa: E402


def test_parse_rules():
    assert schedule.parse("9:30, 12:00 1430", "kr") == (["09:30", "12:00", "14:30"], None)
    assert schedule.parse("12:00,12:00,9:30", "kr")[0] == ["09:30", "12:00"]
    for bad in ("08:00", "15:30", "25:00", "abc", "", "10:00, 10:10"):
        assert schedule.parse(bad, "kr")[1], bad
    assert schedule.parse("10:00, 12:30, 15:00", "us")[1] is None
    assert schedule.parse("16:00", "us")[1]
    assert schedule.times({}, "kr") == ["10:00", "14:30"]
    assert schedule.times({"schedule": {"kr": ["09:30"]}}, "kr") == ["09:30"]


def test_apply_writes_dropin():
    d = Path(tempfile.mkdtemp())
    schedule.SYSTEMD = d
    assert "설치돼 있지 않아" in schedule.apply("kr", ["09:30"])
    (d / "ai-trader.timer").write_text("x")
    calls = []
    schedule.subprocess.run = lambda args, **kw: calls.append(args)
    schedule.shutil.which = lambda x: "/bin/systemctl"
    msg = schedule.apply("kr", ["09:30", "12:00", "14:30"])
    conf = (d / "ai-trader.timer.d" / "schedule.conf").read_text()
    assert "OnCalendar=\n" in conf and conf.count("Asia/Seoul") == 3 and "12:00:00" in conf
    assert ["systemctl", "restart", "ai-trader.timer"] in calls and "적용" in msg


def test_apply_watch_installs_and_toggles():
    d = Path(tempfile.mkdtemp())
    schedule.SYSTEMD = d
    calls = []
    schedule.subprocess.run = lambda args, **kw: calls.append(args)
    schedule.shutil.which = lambda x: "/bin/systemctl"
    msg = schedule.apply_watch(10, ["kr"])
    assert (d / "ai-trader-watch@.service").exists() and (d / "ai-trader-watch-kr.timer").exists()
    conf = (d / "ai-trader-watch-kr.timer.d" / "interval.conf").read_text()
    assert "09..15:00/10:00 Asia/Seoul" in conf
    assert ["systemctl", "enable", "--now", "ai-trader-watch-kr.timer"] in calls
    assert ["systemctl", "disable", "--now", "ai-trader-watch-us.timer"] in calls
    assert "국내 켬" in msg and "미국 끔" in msg
    calls.clear()
    schedule.apply_watch(0, ["kr", "us"])
    assert ["systemctl", "disable", "--now", "ai-trader-watch-kr.timer"] in calls
    assert schedule.watch_minutes({"schedule": {"watch_minutes": 7}}) == 15
    assert schedule.watch_minutes({"schedule": {"watch_minutes": 0}}) == 0


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
