"""자동 실행 시각 — config.toml [schedule] → systemd 타이머 drop-in 으로 적용.

  [schedule]
  kr = ["10:00", "14:30"]        # 한국시간, 평일
  us = ["10:30", "15:00"]        # 뉴욕시간, 평일 (서머타임 자동)

웹에서 저장하면 /etc/systemd/system/<타이머>.d/schedule.conf 를 쓰고 타이머를 다시 읽는다.
(기본 타이머 파일은 그대로 두고 덮어쓰기만 하므로 업데이트해도 유지된다)
"""
from __future__ import annotations

import re
import shutil
import subprocess
from datetime import time
from pathlib import Path

DEFAULT = {"kr": ["10:00", "14:30"], "us": ["10:30", "15:00"]}
TZ = {"kr": "Asia/Seoul", "us": "America/New_York"}
TIMER = {"kr": "ai-trader.timer", "us": "ai-trader-us.timer"}
# 정규장 안에서만 (장 시작 직후·마감 직전 10분은 피함)
WINDOW = {"kr": (time(9, 10), time(15, 10)), "us": (time(9, 40), time(15, 50))}
LABEL = {"kr": "국내(한국시간 09:10~15:10)", "us": "미국(뉴욕시간 09:40~15:50)"}
SYSTEMD = Path("/etc/systemd/system")


def times(cfg_raw: dict, market: str) -> list[str]:
    v = (cfg_raw.get("schedule") or {}).get(market)
    return list(v) if isinstance(v, list) and v else list(DEFAULT[market])


def as_tuples(ts: list[str]) -> list[tuple[int, int]]:
    return [(int(t[:2]), int(t[3:5])) for t in ts]


def parse(text: str, market: str) -> tuple[list[str], str | None]:
    """'9:30, 12:00 14:30' → ['09:30','12:00','14:30'] 와 오류 메시지."""
    out = []
    for tok in re.split(r"[\s,]+", (text or "").strip()):
        if not tok:
            continue
        m = re.fullmatch(r"(\d{1,2}):?(\d{2})", tok)
        if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
            return [], f"{LABEL[market]}: '{tok}' 는 시각 형식이 아닙니다 (예: 09:30)"
        t = time(int(m.group(1)), int(m.group(2)))
        lo, hi = WINDOW[market]
        if not lo <= t <= hi:
            return [], f"{LABEL[market]} 범위 밖의 시각: {t:%H:%M}"
        out.append(f"{t:%H:%M}")
    out = sorted(set(out))
    if not 1 <= len(out) <= 6:
        return [], f"{LABEL[market]}: 하루 1~6번까지 넣을 수 있어요"
    for a, b in zip(out, out[1:]):
        ma, mb = as_tuples([a, b])
        if (mb[0] * 60 + mb[1]) - (ma[0] * 60 + ma[1]) < 30:
            return [], f"{LABEL[market]}: 실행 간격은 30분 이상이어야 해요 ({a}, {b})"
    return out, None


def apply(market: str, ts: list[str]) -> str:
    """설치된 타이머에 반영. 반환: 사람이 읽는 결과 메시지 (실패해도 예외 없이)."""
    timer = TIMER[market]
    if not (SYSTEMD / timer).exists():
        return f"{timer} 가 설치돼 있지 않아 설정만 저장했어요 (README의 자동 실행 설치 후 다시 저장하면 적용)"
    if not shutil.which("systemctl"):
        return "systemctl 을 찾을 수 없어 설정만 저장했어요"
    d = SYSTEMD / f"{timer}.d"
    try:
        d.mkdir(exist_ok=True)
        lines = ["# 웹 설정에서 자동 생성 — 자동 실행 시각", "[Timer]", "OnCalendar="]
        lines += [f"OnCalendar=Mon..Fri *-*-* {t}:00 {TZ[market]}" for t in ts]
        (d / "schedule.conf").write_text("\n".join(lines) + "\n")
        subprocess.run(["systemctl", "daemon-reload"], check=True, timeout=30)
        subprocess.run(["systemctl", "restart", timer], check=True, timeout=30)
    except Exception as e:  # noqa: BLE001
        return f"타이머 적용 실패 ({e}) — 설정은 저장됐어요"
    return f"{timer} 에 적용했어요"


SERVICE = {"kr": "ai-trader.service", "us": "ai-trader-us.service"}


def timer_state(market: str) -> dict:
    """자동 실행 타이머 상태 — {"state": active|inactive|missing|unknown, "next": "MM-DD HH:MM" 또는 None}.
    파일 위치와 상관없이 systemd 에 실제로 등록·실행 중인지 본다."""
    if not shutil.which("systemctl"):
        return {"state": "unknown", "next": None}
    try:
        r = subprocess.run(["systemctl", "show", TIMER[market], "-p", "LoadState", "-p", "ActiveState",
                            "-p", "NextElapseUSecRealtime"], capture_output=True, text=True, timeout=10)
        kv = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)
    except Exception:  # noqa: BLE001
        return {"state": "unknown", "next": None}
    if kv.get("LoadState") != "loaded":
        return {"state": "missing", "next": None}
    nxt = kv.get("NextElapseUSecRealtime") or None
    return {"state": "active" if kv.get("ActiveState") == "active" else "inactive", "next": nxt}


def enable_timer(market: str, ts: list[str]) -> str:
    """자동 실행 타이머 설치(없으면 deploy/ 에서 복사)·켜기·시각 적용. 반환: 결과 메시지 (예외 없음)."""
    from .config import ROOT
    if not shutil.which("systemctl") or not SYSTEMD.exists():
        return "systemctl 이 없어 켤 수 없어요 (서버에서 직접 설치하세요)"
    try:
        for f in (SERVICE[market], TIMER[market]):
            src, dst = ROOT / "deploy" / f, SYSTEMD / f
            if src.exists() and not dst.exists():
                shutil.copy(src, dst)
        subprocess.run(["systemctl", "daemon-reload"], check=True, timeout=30)
        subprocess.run(["systemctl", "enable", "--now", TIMER[market]], check=True, timeout=30)
    except Exception as e:  # noqa: BLE001
        return f"자동 실행 켜기 실패: {e}"
    msg = apply(market, ts)
    st = timer_state(market)
    return f"{'국내' if market == 'kr' else '미국'} 자동 실행을 켰어요 ({msg})" + (
        "" if st["state"] == "active" else f" — 그런데 상태가 {st['state']} 예요. 서버에서 확인이 필요해요")


# ── 장중 감시 (AI 없이 손절·익절만) ─────────────────────────
WATCH_CHOICES = (0, 10, 15, 20, 30)          # 분 (0 = 끄기)
WATCH_TIMER = {"kr": "ai-trader-watch-kr.timer", "us": "ai-trader-watch-us.timer"}
WATCH_SERVICE = "ai-trader-watch@.service"


def watch_minutes(cfg_raw: dict) -> int:
    v = (cfg_raw.get("schedule") or {}).get("watch_minutes", 15)
    try:
        v = int(v)
    except (TypeError, ValueError):
        v = 15
    return v if v in WATCH_CHOICES else 15


def apply_watch(minutes: int, markets: list[str]) -> str:
    """장중 감시 타이머 설치·간격 적용·켜기/끄기. markets: 켤 시장 (나머지는 끔)."""
    from .config import ROOT
    if not shutil.which("systemctl") or not SYSTEMD.exists():
        return "systemctl 이 없어 설정만 저장했어요"
    try:
        for f in [WATCH_SERVICE, *WATCH_TIMER.values()]:
            src, dst = ROOT / "deploy" / f, SYSTEMD / f
            if src.exists() and (not dst.exists() or dst.read_bytes() != src.read_bytes()):
                shutil.copy(src, dst)
        msgs = []
        for mk, timer in WATCH_TIMER.items():
            on = minutes > 0 and mk in markets
            d = SYSTEMD / f"{timer}.d"
            d.mkdir(exist_ok=True)
            hours = "09..15"
            (d / "interval.conf").write_text(
                "# 웹 설정에서 자동 생성 — 장중 감시 간격\n[Timer]\nOnCalendar=\n"
                f"OnCalendar=Mon..Fri *-*-* {hours}:00/{minutes or 15}:00 {TZ[mk]}\n")
            subprocess.run(["systemctl", "daemon-reload"], check=True, timeout=30)
            subprocess.run(["systemctl", "enable" if on else "disable", "--now", timer], check=True, timeout=30)
            if on:
                subprocess.run(["systemctl", "restart", timer], check=True, timeout=30)
            msgs.append(f"{'국내' if mk == 'kr' else '미국'} {'켬' if on else '끔'}")
        return "장중 감시 " + ", ".join(msgs)
    except Exception as e:  # noqa: BLE001
        return f"장중 감시 타이머 적용 실패 ({e}) — 설정은 저장됐어요"
