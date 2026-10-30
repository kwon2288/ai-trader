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


def test_restore_from_list_and_upload_with_safety():
    import io
    import zipfile as zf
    from web.app import app
    app.config["TESTING"] = True                               # 복구 뒤 웹 재시작(프로세스 종료) 막기
    c = cfgmod.load()
    j = Journal(c.db_path)
    j.set("marker", "A")
    p, _ = backup.make(c.data_dir, {})
    p2, _ = backup.make(c.data_dir, {})
    assert p2.name != p.name                                   # 같은 초여도 덮어쓰지 않음
    j.set("marker", "B")                                       # 백업 뒤에 바뀐 상태
    cl = app.test_client()
    with cl.session_transaction() as s:
        s["ok"] = True
    cl.post("/backup/restore", data={"name": p.name, "password": "wrong", "phrase": "복구"})
    assert Journal(c.db_path).get("marker") == "B"
    cl.post("/backup/restore", data={"name": p.name, "password": "secret-pass", "phrase": "되돌려"})
    assert Journal(c.db_path).get("marker") == "B"
    r = cl.post("/backup/restore", data={"name": p.name, "password": "secret-pass", "phrase": "복구"})
    assert "복구했습니다" in r.get_data(as_text=True)
    assert Journal(c.db_path).get("marker") == "A"
    assert len(backup.listing(c.data_dir)) >= 1                  # 직전 상태 백업이 남음
    # 다른 파일이 섞인 zip 은 거부
    b = io.BytesIO()
    with zf.ZipFile(b, "w") as z:
        z.writestr("data/journal.db", p.read_bytes()[:10])
        z.writestr("../../etc/passwd", "x")
    r = cl.post("/backup/restore", data={"password": "secret-pass", "phrase": "복구",
                                         "file": (io.BytesIO(b.getvalue()), "evil.zip")},
                content_type="multipart/form-data")
    assert "복구했습니다" not in r.get_data(as_text=True)
    assert Journal(c.db_path).get("marker") == "A"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
