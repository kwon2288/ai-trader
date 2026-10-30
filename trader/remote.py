"""백업 원격 저장 — FTP / FTPS / WebDAV (NAS·클라우드 드라이브).

config [backup]
  remote_type = "ftp" | "ftps" | "webdav" | ""   (빈 값 = 안 씀)
  remote_url  = "ftp://nas.local:21/backup/ai-trader"  또는  "https://nas.local:5006/backup/ai-trader"
  remote_user = "사용자"
.env
  BACKUP_REMOTE_PASSWORD = 비밀번호   (웹 설정 → 백업에서 넣으면 .env 에 저장, 화면엔 다시 안 보임)

보관: 파일 이름의 날짜(ai-trader-YYYYMMDD-...) 기준으로 keep_days 가 지난 원격 백업도 지운다.
"""
from __future__ import annotations

import ftplib
import io
import os
import re
import ssl
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

import requests

NAME_RE = re.compile(r"ai-trader-(\d{8})-\d{6}\.zip$")
TIMEOUT = 30
TYPES = {"": "사용 안 함", "ftp": "FTP", "ftps": "FTPS (FTP over TLS)", "webdav": "WebDAV (https/http)"}


def settings(raw: dict) -> dict:
    b = raw.get("backup") or {}
    t = str(b.get("remote_type") or "").lower()
    return {"type": t if t in TYPES else "", "url": str(b.get("remote_url") or "").strip(),
            "user": str(b.get("remote_user") or "").strip(),
            "password": os.environ.get("BACKUP_REMOTE_PASSWORD", "")}


def check_url(t: str, url: str) -> str | None:
    if not t:
        return None
    u = urlparse(url)
    if t in ("ftp", "ftps") and u.scheme not in ("ftp", "ftps"):
        return "FTP 주소는 ftp://호스트[:포트]/폴더 형식이어야 합니다"
    if t == "webdav" and u.scheme not in ("http", "https"):
        return "WebDAV 주소는 https://호스트[:포트]/폴더 형식이어야 합니다"
    if not u.hostname:
        return "주소에 호스트가 없습니다"
    return None


# ── FTP / FTPS ──
def _ftp(st: dict):
    u = urlparse(st["url"])
    if st["type"] == "ftps":
        f = ftplib.FTP_TLS(context=ssl.create_default_context(), timeout=TIMEOUT)
    else:
        f = ftplib.FTP(timeout=TIMEOUT)
    f.connect(u.hostname, u.port or 21)
    f.login(st["user"] or "anonymous", st["password"] or "")
    if st["type"] == "ftps":
        f.prot_p()
    path = unquote(u.path or "/")
    for part in [p for p in path.split("/") if p]:     # 폴더가 없으면 만든다
        try:
            f.cwd(part)
        except ftplib.error_perm:
            f.mkd(part)
            f.cwd(part)
    return f


def _ftp_upload(st, name: str, data: bytes) -> None:
    f = _ftp(st)
    try:
        f.storbinary(f"STOR {name}", io.BytesIO(data))
    finally:
        f.quit()


def _ftp_list(st) -> list[str]:
    f = _ftp(st)
    try:
        return [n.rsplit("/", 1)[-1] for n in f.nlst()]
    finally:
        f.quit()


def _ftp_delete(st, names: list[str]) -> None:
    f = _ftp(st)
    try:
        for n in names:
            f.delete(n)
    finally:
        f.quit()


# ── WebDAV ──
def _dav_base(st) -> str:
    return st["url"].rstrip("/") + "/"


def _dav(st, method: str, url: str, **kw):
    r = requests.request(method, url, auth=(st["user"], st["password"]) if st["user"] else None,
                         timeout=TIMEOUT, **kw)
    return r


def _dav_mkdirs(st) -> None:
    u = urlparse(st["url"])
    root = f"{u.scheme}://{u.netloc}"
    cur = ""
    for part in [p for p in u.path.split("/") if p]:
        cur += "/" + part
        r = _dav(st, "PROPFIND", root + cur + "/", headers={"Depth": "0"})
        if r.status_code == 404:
            m = _dav(st, "MKCOL", root + cur + "/")
            if m.status_code not in (200, 201, 405):
                raise RuntimeError(f"WebDAV 폴더 만들기 실패 {m.status_code}: {cur}")


def _dav_upload(st, name: str, data: bytes) -> None:
    r = _dav(st, "PUT", _dav_base(st) + quote(name), data=data)
    if r.status_code == 409:                            # 폴더 없음 → 만들고 다시
        _dav_mkdirs(st)
        r = _dav(st, "PUT", _dav_base(st) + quote(name), data=data)
    if r.status_code not in (200, 201, 204):
        raise RuntimeError(f"WebDAV 업로드 실패 {r.status_code}: {r.text[:150]}")


def _dav_list(st) -> list[str]:
    r = _dav(st, "PROPFIND", _dav_base(st), headers={"Depth": "1"})
    if r.status_code not in (200, 207):
        raise RuntimeError(f"WebDAV 목록 실패 {r.status_code}")
    return [unquote(h).rstrip("/").rsplit("/", 1)[-1] for h in re.findall(r"<[^>]*href>([^<]+)</[^>]*href>", r.text)]


def _dav_delete(st, names: list[str]) -> None:
    for n in names:
        _dav(st, "DELETE", _dav_base(st) + quote(n))


# ── 공통 ──
def upload(raw: dict, path: Path, keep_days: int) -> str | None:
    """설정돼 있으면 올리고 오래된 원격 백업을 지운다. 반환: 메모(성공) — 실패면 예외."""
    st = settings(raw)
    if not st["type"] or not st["url"]:
        return None
    data = Path(path).read_bytes()
    if st["type"] in ("ftp", "ftps"):
        _ftp_upload(st, path.name, data)
        names = _ftp_list(st)
    else:
        _dav_upload(st, path.name, data)
        names = _dav_list(st)
    cut = (datetime.now() - timedelta(days=keep_days)).strftime("%Y%m%d")
    old = [n for n in names if (m := NAME_RE.search(n)) and m.group(1) < cut]
    if old:
        (_ftp_delete if st["type"] in ("ftp", "ftps") else _dav_delete)(st, old)
    return f"{TYPES[st['type']].split(' ')[0]} 업로드" + (f", 오래된 {len(old)}개 정리" if old else "")


def test(raw: dict) -> str:
    """작은 파일을 올렸다 지워 연결을 확인."""
    st = settings(raw)
    if not st["type"]:
        raise RuntimeError("원격 저장이 꺼져 있습니다")
    err = check_url(st["type"], st["url"])
    if err:
        raise RuntimeError(err)
    name = f"ai-trader-test-{int(time.time())}.txt"
    if st["type"] in ("ftp", "ftps"):
        _ftp_upload(st, name, b"ok")
        _ftp_delete(st, [name])
    else:
        _dav_upload(st, name, b"ok")
        _dav_delete(st, [name])
    return "연결 성공 — 테스트 파일을 올렸다가 지웠어요"


def listing(raw: dict) -> list[str]:
    """원격에 있는 백업 파일 이름 (최신 먼저)."""
    st = settings(raw)
    if not st["type"] or not st["url"]:
        return []
    names = _ftp_list(st) if st["type"] in ("ftp", "ftps") else _dav_list(st)
    return sorted({n for n in names if NAME_RE.search(n)}, reverse=True)


def download(raw: dict, name: str, dest: Path) -> Path:
    if not NAME_RE.search(name):
        raise ValueError("백업 파일 이름이 아닙니다")
    st = settings(raw)
    if st["type"] in ("ftp", "ftps"):
        f = _ftp(st)
        try:
            with open(dest, "wb") as out:
                f.retrbinary(f"RETR {name}", out.write)
        finally:
            f.quit()
    elif st["type"] == "webdav":
        r = _dav(st, "GET", _dav_base(st) + quote(name))
        if r.status_code != 200:
            raise RuntimeError(f"WebDAV 내려받기 실패 {r.status_code}")
        Path(dest).write_bytes(r.content)
    else:
        raise RuntimeError("원격 저장이 꺼져 있습니다")
    return Path(dest)
