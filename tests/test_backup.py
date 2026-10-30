"""백업 — zip 내용, .env 제외, 추가 위치 복사, 보관 기간, 하루 한 번."""
import os
import sys
import tempfile
import time
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.update(TRADER_DATA_DIR=tempfile.mkdtemp(), WEB_PASSWORD="secret-pass", TRADING_MODE="dry")
from trader import backup, config as cfgmod  # noqa: E402
from trader.journal import Journal  # noqa: E402


def test_make_contents_and_prune():
    c = cfgmod.load()
    j = Journal(c.db_path)
    j.set("x", "1")
    (c.data_dir / "mode.json").write_text('{"mode":"mock"}')
    extra = Path(tempfile.mkdtemp()) / "nas"
    p, notes = backup.make(c.data_dir, {"backup": {"extra_dir": str(extra), "keep_days": 3}})
    names = zipfile.ZipFile(p).namelist()
    assert "data/journal.db" in names and "data/mode.json" in names and ".env" not in names
    assert (extra / p.name).exists() and oct(p.stat().st_mode)[-3:] == "600"
    old = backup.folder(c.data_dir) / "ai-trader-20000101-000000.zip"
    old.write_text("x")
    os.utime(old, (time.time() - 10 * 86400,) * 2)
    backup.make(c.data_dir, {"backup": {"keep_days": 3}})
    assert not old.exists() and backup.listing(c.data_dir)


def test_daily_once():
    c = cfgmod.load()
    j = Journal(c.db_path)
    j.set("backup_last", "")
    assert backup.daily(j, c.data_dir, {}) is not None
    assert backup.daily(j, c.data_dir, {}) is None


def test_web_backup_and_download():
    from web.app import app
    cl = app.test_client()
    with cl.session_transaction() as s:
        s["ok"] = True
    cl.post("/backup/now")
    name = backup.listing(cfgmod.load().data_dir)[0]["name"]
    r = cl.get(f"/backup/{name}")
    assert r.status_code == 200 and r.data[:2] == b"PK"
    assert cl.get("/backup/..%2F..%2Fetc%2Fpasswd").status_code == 404
    assert app.test_client().get(f"/backup/{name}").status_code == 302      # 로그인 필요


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
