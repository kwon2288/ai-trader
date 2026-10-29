"""실행 모드(dry/mock/live)를 웹에서 바꿀 때 저장하는 파일 — data/mode.json.

이 파일이 있으면 .env 의 TRADING_MODE 보다 우선한다. 지우면 다시 .env 값을 쓴다.
실계좌(live)는 웹에서 비밀번호 재입력 + 확인 문구를 거쳐야 live_ack=true 로 저장된다.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

MODES = ("dry", "mock", "live")


def path(data_dir: Path) -> Path:
    return Path(data_dir) / "mode.json"


def read(data_dir: Path) -> dict | None:
    p = path(data_dir)
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) and d.get("mode") in MODES else None


def write(data_dir: Path, mode: str, live_ack: bool = False, by: str = "web") -> dict:
    if mode not in MODES:
        raise ValueError(f"모드는 {MODES} 중 하나")
    d = {"mode": mode, "live_ack": bool(live_ack and mode == "live"), "by": by,
         "ts": datetime.now(ZoneInfo("Asia/Seoul")).isoformat(timespec="seconds")}
    p = path(data_dir)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(p)
    return d


def clear(data_dir: Path) -> None:
    path(data_dir).unlink(missing_ok=True)
