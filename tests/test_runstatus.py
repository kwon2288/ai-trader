"""실행 상태 표시 · 멈춘 실행 정리 · 다수결 시간 제한."""
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from trader import analyst, runstatus  # noqa: E402
import test_bot as tb  # noqa: E402


def test_status_lifecycle():
    d = Path(tempfile.mkdtemp())
    runstatus.start(d, "정기 매매", "us")
    runstatus.step(d, "🧠 AI 판단 요청 중…\n둘째 줄")
    s = runstatus.read(d)
    assert s["what"] == "정기 매매" and s["step"] == "🧠 AI 판단 요청 중…" and not s["done"]
    runstatus.end(d)
    assert runstatus.read(d)["done"] is True
    runstatus.step(d, "끝난 뒤")                       # 끝난 실행은 덮어쓰지 않음
    assert runstatus.read(d)["step"] == "🧠 AI 판단 요청 중…"
    assert runstatus.alive(os.getpid()) and not runstatus.alive(999999999)


def test_bot_reports_steps():
    cfg, br, _, bot = tb.setup(decisions=[])
    runstatus.start(cfg.data_dir, "매매 실행", "kr")
    bot.run()
    assert runstatus.read(cfg.data_dir)["step"]


def test_consensus_deadline():
    class Slow:
        label = "느림"

        def decide(self, ctx):
            time.sleep(3)
            return {}

    class Fast:
        def __init__(self, n):
            self.label = n

        def decide(self, ctx):
            return {"market_view": "x", "decisions": [], "_provider": self.label, "_usage": None}
    old = analyst.CONSENSUS_DEADLINE
    analyst.CONSENSUS_DEADLINE = 1
    try:
        t = time.time()
        out = analyst.ConsensusAnalyst([Fast("A"), Fast("B"), Slow()]).decide({})
        assert time.time() - t < 2.5 and "응답 없음" in out["market_view"]
    finally:
        analyst.CONSENSUS_DEADLINE = old


def test_web_busy_info_and_stop():
    os.environ.update(TRADER_DATA_DIR=tempfile.mkdtemp(), WEB_PASSWORD="secret-pass")
    import fcntl
    import web.app as W
    W.app.config["TESTING"] = True
    c = W.cfg()
    cl = W.app.test_client()
    with cl.session_transaction() as s:
        s["ok"] = True
    fh = open(c.data_dir / "run.lock", "w")
    fcntl.flock(fh, fcntl.LOCK_EX)                     # 다른 프로세스가 잡은 것처럼(같은 프로세스지만 다른 파일 핸들)
    try:
        runstatus.start(c.data_dir, "정기 매매", "us")
        info = W._busy_info(c)
        assert info and info["what"] == "정기 매매" and not info["stale"]
        r = cl.post("/run/stop")
        assert r.status_code == 302                    # 아직 15분 전 → 거부
        st = runstatus.read(c.data_dir)
        st["started"] = time.time() - 20 * 60
        runstatus._write(c.data_dir, st)
        assert W._busy_info(c)["stale"]
        js = cl.get("/api/status").get_json()
        assert js["busy"] and js["info"]["stale"]
        h = cl.get("/").get_data(as_text=True)
        assert "멈춘 실행 정리" in h and "정기 매매" in h
        r = cl.post("/run/stop")                       # 같은 프로세스 → 웹 재시작 경로 (TESTING 이라 종료 안 함)
        assert r.status_code == 200
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
