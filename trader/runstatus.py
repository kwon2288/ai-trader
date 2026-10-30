"""지금 무엇이 실행 중인지 — 웹 '실행 중' 표시와 멈춘 실행 정리에 쓴다.

data/run_status.json = {"pid", "what", "market", "started"(epoch), "step", "step_ts", "done"}
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

FILE = "run_status.json"
STALE_MIN = 15          # 이만큼 넘게 걸리면 '멈췄을 수 있음'


def _path(data_dir) -> Path:
    return Path(data_dir) / FILE


def _write(data_dir, d: dict) -> None:
    p = _path(data_dir)
    tmp = p.with_name(p.name + ".tmp")
    try:
        tmp.write_text(json.dumps(d, ensure_ascii=False))
        os.replace(tmp, p)
    except OSError:
        pass


def read(data_dir) -> dict:
    try:
        return json.loads(_path(data_dir).read_text())
    except (OSError, ValueError):
        return {}


def start(data_dir, what: str, market: str = "") -> None:
    now = time.time()
    _write(data_dir, {"pid": os.getpid(), "what": what, "market": market, "started": now,
                      "step": "시작", "step_ts": now, "done": False})


def step(data_dir, text: str) -> None:
    d = read(data_dir)
    if not d or d.get("done") or d.get("pid") != os.getpid():
        return
    d.update(step=str(text).split("\n")[0][:120], step_ts=time.time())
    _write(data_dir, d)


def end(data_dir) -> None:
    d = read(data_dir)
    if d and d.get("pid") == os.getpid():
        d["done"] = True
        _write(data_dir, d)


def alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def is_trader_process(pid) -> bool:
    """우리 봇(run.py) 프로세스인지 — 엉뚱한 프로세스를 멈추지 않게."""
    try:
        cmd = Path(f"/proc/{int(pid)}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="ignore")
    except (OSError, ValueError, TypeError):
        return False
    return "run.py" in cmd
