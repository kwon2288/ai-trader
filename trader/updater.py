"""웹에서 GitHub 최신 버전 확인·적용 (deploy/update.sh 를 그대로 사용)."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from pathlib import Path

from . import __version__
from .config import ROOT

LOG_NAME = "update.log"
CHECK_NAME = "update_check.json"


def _git(*args: str, timeout: int = 60) -> str:
    r = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip()[-500:] or f"git {' '.join(args)} 실패")
    return r.stdout


def _new_sections(changelog: str, current: str) -> str:
    """CHANGELOG 에서 현재 버전보다 위(새 버전)에 있는 부분만."""
    out, started = [], False
    for line in changelog.splitlines():
        m = re.match(r"^##\s+v([\d.]+)", line)
        if m:
            if m.group(1) == current:
                break
            started = True
        if started:
            out.append(line)
    return "\n".join(out).strip()


def check(data_dir: Path) -> dict:
    res = {"ts": time.strftime("%Y-%m-%d %H:%M"), "current": __version__}
    try:
        _git("fetch", "--quiet", "origin")
        head, remote = _git("rev-parse", "HEAD").strip(), _git("rev-parse", "origin/main").strip()
        res["behind"] = int(_git("rev-list", "--count", f"HEAD..origin/main").strip() or 0)
        res["head"], res["remote"] = head[:7], remote[:7]
        if res["behind"]:
            init = _git("show", "origin/main:trader/__init__.py")
            m = re.search(r'__version__\s*=\s*"([^"]+)"', init)
            res["latest"] = m.group(1) if m else remote[:7]
            try:
                res["notes"] = _new_sections(_git("show", "origin/main:CHANGELOG.md"), __version__)
            except RuntimeError:
                res["notes"] = ""
            res["commits"] = _git("log", "--format=%h %s", "HEAD..origin/main").strip().splitlines()[:20]
        dirty = _git("status", "--porcelain", "--untracked-files=no").strip()
        if dirty:
            res["dirty"] = dirty.splitlines()[:10]   # 서버에서 직접 고친 파일 → pull 이 실패할 수 있음
    except Exception as e:  # noqa: BLE001
        res["error"] = str(e)
    (Path(data_dir) / CHECK_NAME).write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
    return res


def last_check(data_dir: Path) -> dict | None:
    try:
        d = json.loads((Path(data_dir) / CHECK_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if d.get("current") != __version__:          # 그 사이 업데이트됐으면 예전 확인 결과는 버림
        return None
    return d


def start(data_dir: Path) -> None:
    """update.sh 를 웹 프로세스 밖에서 실행 (웹이 재시작돼도 끊기지 않게)."""
    log = Path(data_dir) / LOG_NAME
    log.write_text(f"[{time.strftime('%H:%M:%S')}] 업데이트 시작\n", encoding="utf-8")
    script = f'bash deploy/update.sh >> "{log}" 2>&1; echo "__EXIT=$?" >> "{log}"'
    if shutil.which("systemd-run"):
        subprocess.run(["systemd-run", f"--unit=ai-trader-update-{int(time.time())}", "--collect", "--quiet",
                        f"--working-directory={ROOT}", "bash", "-c", script], check=True, timeout=30)
    else:
        subprocess.Popen(["bash", "-c", script], cwd=ROOT, start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def log_text(data_dir: Path) -> tuple[str, bool, int | None]:
    """(로그 내용, 끝났는지, 종료코드)"""
    try:
        t = (Path(data_dir) / LOG_NAME).read_text(encoding="utf-8")
    except OSError:
        return "", True, None
    m = re.search(r"__EXIT=(\d+)", t)
    return re.sub(r"__EXIT=\d+\n?", "", t), bool(m), int(m.group(1)) if m else None
