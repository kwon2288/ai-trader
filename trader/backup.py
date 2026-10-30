"""매매 기록 백업 — 하루 한 번 자동 + 웹에서 수동.

zip 안에: journal.db(매매·판단·손익 기록, 실행 중에도 안전한 SQLite 백업), config.toml(설정),
         mode.json·ai_cost.json·passkeys.json(웹 설정). .env(앱키·비밀번호)는 기본으로 넣지 않는다.
저장 위치: data/backups/  (+ extra_dir 로 다른 디스크에 복사, + FTP/FTPS/WebDAV 로 NAS·클라우드에 업로드 — trader/remote.py)
보관: keep_days 일이 지난 백업은 지운다.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import time
import zipfile
from datetime import datetime
from pathlib import Path

from .config import ROOT
from .journal import KST

FILES = ["mode.json", "ai_cost.json", "passkeys.json"]


def settings(raw: dict) -> dict:
    b = raw.get("backup") or {}
    try:
        keep = max(1, min(365, int(b.get("keep_days", 14))))
    except (TypeError, ValueError):
        keep = 14
    return {"keep_days": keep, "extra_dir": str(b.get("extra_dir") or "").strip(),
            "include_env": bool(b.get("include_env", False))}


def folder(data_dir: Path) -> Path:
    d = Path(data_dir) / "backups"
    d.mkdir(parents=True, exist_ok=True)
    return d


def make(data_dir: Path, raw: dict) -> tuple[Path, list[str]]:
    """백업 zip 을 만들고 (경로, 메모) 반환."""
    st = settings(raw)
    data_dir = Path(data_dir)
    out = folder(data_dir) / f"ai-trader-{datetime.now(KST):%Y%m%d-%H%M%S}.zip"
    notes = []
    with tempfile.TemporaryDirectory() as tmp:
        db = data_dir / "journal.db"
        if db.exists():
            snap = Path(tmp) / "journal.db"
            src, dst = sqlite3.connect(db), sqlite3.connect(snap)
            with dst:
                src.backup(dst)                 # 쓰는 중이어도 일관된 사본
            src.close()
            dst.close()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            if db.exists():
                z.write(Path(tmp) / "journal.db", "data/journal.db")
            for f in FILES:
                if (data_dir / f).exists():
                    z.write(data_dir / f, f"data/{f}")
            if (ROOT / "config.toml").exists():
                z.write(ROOT / "config.toml", "config.toml")
            if st["include_env"] and (ROOT / ".env").exists():
                z.write(ROOT / ".env", ".env")
                notes.append(".env 포함")
    os.chmod(out, 0o600)
    if st["extra_dir"]:
        try:
            ext = Path(st["extra_dir"])
            ext.mkdir(parents=True, exist_ok=True)
            shutil.copy2(out, ext / out.name)
            notes.append(f"{ext} 에도 복사")
            prune(ext, st["keep_days"])
        except Exception as e:  # noqa: BLE001
            notes.append(f"추가 위치 복사 실패: {e}")
    try:                                             # FTP / WebDAV
        from . import remote
        m = remote.upload(raw, out, st["keep_days"])
        if m:
            notes.append(m)
    except Exception as e:  # noqa: BLE001
        notes.append(f"원격 업로드 실패: {e}")
    prune(folder(data_dir), st["keep_days"])
    return out, notes


def prune(d: Path, keep_days: int) -> int:
    cut = time.time() - keep_days * 86400
    n = 0
    for f in Path(d).glob("ai-trader-*.zip"):
        if f.stat().st_mtime < cut:
            f.unlink(missing_ok=True)
            n += 1
    return n


def listing(data_dir: Path) -> list[dict]:
    return [{"name": f.name, "size": f.stat().st_size,
             "ts": datetime.fromtimestamp(f.stat().st_mtime, KST).strftime("%m-%d %H:%M")}
            for f in sorted(folder(data_dir).glob("ai-trader-*.zip"), reverse=True)]


def daily(j, data_dir: Path, raw: dict, warn=None) -> Path | None:
    """오늘 백업이 없으면 하나 만든다 (자동 실행·웹이 부름). warn(msg): 원격·추가 위치 실패 알림."""
    today = datetime.now(KST).date().isoformat()
    if j.get("backup_last") == today:
        return None
    p, notes = make(data_dir, raw)
    j.set("backup_last", today)
    bad = [n for n in notes if "실패" in n]
    if bad and warn:
        warn("💾 백업은 서버에 저장됐지만 " + "; ".join(bad))
    return p
