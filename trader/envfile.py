""".env 파일을 주석·순서를 지키며 고쳐 쓰기 (웹 '연결 정보'와 `run.py passwd` 에서 사용)."""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from .config import ROOT

ENV_PATH = ROOT / ".env"
EXAMPLE = ROOT / ".env.example"
_KEY = re.compile(r"^\s*(#\s*)?([A-Z][A-Z0-9_]*)\s*=")


def ensure(path: Path = ENV_PATH) -> Path:
    if not path.exists():
        if EXAMPLE.exists():
            shutil.copy(EXAMPLE, path)
        else:
            path.write_text("", encoding="utf-8")
        os.chmod(path, 0o600)
    return path


def read(path: Path = ENV_PATH) -> dict[str, str]:
    out = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        v = v.strip()
        if v[:1] not in ('"', "'"):
            v = v.split(" #", 1)[0].strip()        # 줄 끝 주석 제거
        out[k.strip()] = v.strip('"').strip("'")
    return out


def check_value(v: str) -> str | None:
    if any(ch in v for ch in "\r\n\"'\\") or len(v) > 500:
        return "따옴표·역슬래시·줄바꿈은 넣을 수 없고 500자 이하여야 합니다"
    return None


def _fmt(k: str, v: str) -> str:
    return f'{k}="{v}"' if (" " in v or "#" in v) else f"{k}={v}"


def update(changes: dict[str, str | None], path: Path = ENV_PATH) -> None:
    """changes: 키 → 새 값(None 이면 삭제). 있는 줄은 그 자리에서, 주석 처리된 예시 줄이 있으면
    그 줄을 살려서, 없으면 끝에 추가한다. 쓰기 전 .env.bak 백업, 권한 600."""
    ensure(path)
    lines = path.read_text(encoding="utf-8").splitlines()
    todo = dict(changes)
    # 1) 살아 있는 줄
    for i, line in enumerate(lines):
        m = _KEY.match(line)
        if m and not m.group(1) and m.group(2) in todo:
            k = m.group(2)
            v = todo.pop(k)
            lines[i] = None if v is None else _fmt(k, v)
    lines = [x for x in lines if x is not None]
    # 2) 주석 처리된 예시 줄 (# KEY=...) → 값을 넣어 살린다
    for i, line in enumerate(lines):
        m = _KEY.match(line)
        if m and m.group(1) and todo.get(m.group(2)) is not None:
            k = m.group(2)
            lines[i] = _fmt(k, todo.pop(k))
    # 3) 나머지는 끝에
    for k, v in todo.items():
        if v is not None:
            lines.append(_fmt(k, v))
    bak = path.with_name(path.name + ".bak")
    shutil.copy(path, bak)
    os.chmod(bak, 0o600)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
