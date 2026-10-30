"""백업 원격 저장 — 실제 FTP·WebDAV 서버(개발용 패키지가 있을 때)와 웹 설정 저장."""
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.update(TRADER_DATA_DIR=tempfile.mkdtemp(), WEB_PASSWORD="secret-pass", TRADING_MODE="dry")
from trader import remote  # noqa: E402


def test_url_rules():
    assert remote.check_url("ftp", "ftp://nas/backup") is None
    assert remote.check_url("ftp", "https://nas/backup")
    assert remote.check_url("webdav", "ftp://nas/x")
    assert remote.check_url("webdav", "https:///x")
    assert remote.upload({}, Path("x.zip"), 14) is None                  # 꺼져 있으면 아무것도 안 함


def test_real_ftp_and_webdav_if_available():
    try:
        from cheroot import wsgi
        from pyftpdlib.authorizers import DummyAuthorizer
        from pyftpdlib.handlers import FTPHandler
        from pyftpdlib.servers import FTPServer
        from wsgidav.wsgidav_app import WsgiDAVApp
    except ImportError:
        print("  (pyftpdlib·wsgidav 없음 — 건너뜀)")
        return
    root = Path(tempfile.mkdtemp())
    (root / "ftp").mkdir()
    (root / "dav").mkdir()
    a = DummyAuthorizer()
    a.add_user("u", "p", str(root / "ftp"), perm="elradfmwMT")
    FTPHandler.authorizer = a
    fs = FTPServer(("127.0.0.1", 0), FTPHandler)
    fport = fs.socket.getsockname()[1]
    threading.Thread(target=fs.serve_forever, kwargs={"blocking": True}, daemon=True).start()
    app = WsgiDAVApp({"provider_mapping": {"/": str(root / "dav")},
                      "simple_dc": {"user_mapping": {"*": {"u": {"password": "p"}}}},
                      "http_authenticator": {"accept_basic": True, "accept_digest": False, "default_to_digest": False},
                      "verbose": 0, "logging": {"enable": False}})
    ws = wsgi.Server(("127.0.0.1", 0), app)
    ws.prepare()
    dport = ws.bind_addr[1]
    threading.Thread(target=ws.serve, daemon=True).start()
    time.sleep(0.5)
    os.environ["BACKUP_REMOTE_PASSWORD"] = "p"
    f = root / "ai-trader-20991231-120000.zip"
    f.write_bytes(b"PK")
    try:
        for t, url, sub in [("ftp", f"ftp://127.0.0.1:{fport}/b/ai", "ftp"), ("webdav", f"http://127.0.0.1:{dport}/b/ai", "dav")]:
            raw = {"backup": {"remote_type": t, "remote_url": url, "remote_user": "u"}}
            assert "성공" in remote.test(raw)
            d = root / sub / "b" / "ai"
            d.mkdir(parents=True, exist_ok=True)
            (d / "ai-trader-20000101-000000.zip").write_bytes(b"old")
            assert "정리" in remote.upload(raw, f, 14)
            assert sorted(p.name for p in d.iterdir()) == [f.name], t
    finally:
        os.environ.pop("BACKUP_REMOTE_PASSWORD", None)
        ws.stop()
        fs.close_all()


def test_web_saves_remote_settings_password_in_env():
    import shutil
    import tomllib
    from trader import envfile
    import web.app as W
    tmp = Path(tempfile.mkdtemp())
    cfgp, envp = tmp / "config.toml", tmp / ".env"
    shutil.copy(Path(__file__).resolve().parent.parent / "config.example.toml", cfgp)
    envp.write_text("")
    old = W.CONFIG_PATH
    W.CONFIG_PATH = cfgp
    for fn in (envfile.update, envfile.read, envfile.ensure):
        fn.__defaults__ = (envp,)
    try:
        cl = W.app.test_client()
        with cl.session_transaction() as s:
            s["ok"] = True
        cl.post("/backup/settings", data={"keep_days": "7", "remote_type": "webdav", "remote_url": "ftp://bad"})
        assert tomllib.loads(cfgp.read_text())["backup"].get("remote_type", "") == ""      # 잘못된 주소 거부
        cl.post("/backup/settings", data={"keep_days": "7", "remote_type": "webdav", "remote_user": "me",
                                          "remote_url": "https://nas.local:5006/backup", "remote_password": "pw1"})
        b = tomllib.loads(cfgp.read_text())["backup"]
        assert (b["remote_type"], b["remote_url"], b["remote_user"], b["keep_days"]) == ("webdav", "https://nas.local:5006/backup", "me", 7)
        assert envfile.read()["BACKUP_REMOTE_PASSWORD"] == "pw1" and "pw1" not in cfgp.read_text()
        assert "pw1" not in cl.get("/settings").get_data(as_text=True)
    finally:
        W.CONFIG_PATH = old
        os.environ.pop("BACKUP_REMOTE_PASSWORD", None)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
