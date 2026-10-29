"""웹 업데이트 — 새 버전 확인(변경 사항 추출)과 비밀번호·실행 중 차단."""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.update(TRADER_DATA_DIR=tempfile.mkdtemp(), WEB_PASSWORD="secret-pass", TRADING_MODE="dry")

from trader import __version__, updater  # noqa: E402


def sh(cwd, *a):
    subprocess.run(a, cwd=cwd, check=True, capture_output=True)


def make_repos():
    t = Path(tempfile.mkdtemp())
    sh(t, "git", "init", "-q", "--bare", "-b", "main", "remote.git")
    sh(t, "git", "clone", "-q", "remote.git", "dev")
    dev = t / "dev"
    for k, v in (("user.email", "a@b"), ("user.name", "a")):
        sh(dev, "git", "config", k, v)
    (dev / "trader").mkdir()
    (dev / "trader/__init__.py").write_text(f'__version__ = "{__version__}"\n')
    (dev / "CHANGELOG.md").write_text(f"# 변경\n\n## v{__version__} — x\n- 옛것\n")
    (dev / "deploy").mkdir()
    (dev / "deploy/update.sh").write_text('echo "업데이트 실행됨"\n')
    sh(dev, "git", "add", "-A"); sh(dev, "git", "commit", "-qm", "v1"); sh(dev, "git", "push", "-q", "origin", "main")
    sh(t, "git", "clone", "-q", "remote.git", "server")
    (dev / "trader/__init__.py").write_text('__version__ = "9.9.9"\n')
    (dev / "CHANGELOG.md").write_text(f"# 변경\n\n## v9.9.9 — y\n- 새 기능 A\n\n## v{__version__} — x\n- 옛것\n")
    sh(dev, "git", "commit", "-qam", "v9"); sh(dev, "git", "push", "-q", "origin", "main")
    return t / "server"


def test_check_finds_new_version_and_notes():
    server = make_repos()
    updater.ROOT = server
    d = Path(tempfile.mkdtemp())
    r = updater.check(d)
    assert r.get("error") is None, r
    assert r["behind"] == 1 and r["latest"] == "9.9.9"
    assert "새 기능 A" in r["notes"] and "옛것" not in r["notes"]
    assert updater.last_check(d)["latest"] == "9.9.9"


def test_start_runs_update_script_and_logs():
    server = make_repos()
    updater.ROOT = server
    d = Path(tempfile.mkdtemp())
    old = updater.shutil.which
    updater.shutil.which = lambda x: None          # systemd-run 없는 환경
    try:
        updater.start(d)
        for _ in range(50):
            text, done, code = updater.log_text(d)
            if done:
                break
            time.sleep(0.1)
    finally:
        updater.shutil.which = old
    assert done and code == 0 and "업데이트 실행됨" in text and "__EXIT" not in text


def test_web_apply_needs_password():
    import web.app as webapp
    webapp.app.config["TESTING"] = True
    started = []
    updater.start = lambda d: started.append(d)
    c = webapp.app.test_client()
    with c.session_transaction() as s:
        s["ok"] = True
    c.post("/update/apply", data={"password": "nope"})
    assert not started
    r = c.post("/update/apply", data={"password": "secret-pass"})
    assert started and r.headers["Location"].endswith("/update")
    os.environ["WEB_UPDATE"] = "off"
    try:
        c.post("/update/apply", data={"password": "secret-pass"})
        assert len(started) == 1
    finally:
        os.environ.pop("WEB_UPDATE")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
