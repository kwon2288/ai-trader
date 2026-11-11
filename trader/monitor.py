"""실행 누락 감시 + 외부 헬스체크.

1) 누락 감시 (서버 안에서): 오늘 정해진 자동 실행 시각이 15분 넘게 지났는데 실행 기록이 없으면 알림.
   웹 화면(백그라운드)과 장중 감시가 확인한다. 타이머가 설치된 시장만, 시각마다 한 번만 알린다.
2) 외부 헬스체크 (서버가 통째로 꺼진 경우): .env 의 HEALTHCHECK_URL (예: healthchecks.io 체크 주소)에
   자동 실행이 끝날 때마다 신호를 보낸다. 신호가 끊기면 그 서비스가 메일·텔레그램으로 알려 준다.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import schedule
from .journal import KST

log = logging.getLogger("monitor")
GRACE_MIN = 15


def timer_installed(market: str) -> bool:
    if (schedule.SYSTEMD / schedule.TIMER[market]).exists():
        return True
    return schedule.timer_state(market)["state"] in ("active", "inactive")     # 다른 위치에 설치된 경우


def missed_slots(j, cfg, raw: dict, market: str, now: datetime | None = None) -> list[str]:
    """오늘(그 시장 날짜) 놓친 실행 시각 목록 'HH:MM'."""
    tz = ZoneInfo(schedule.TZ[market])
    now = (now or datetime.now(KST)).astimezone(tz)
    if now.weekday() >= 5:
        return []
    ledger = cfg.for_market(market).ledger if market != cfg.market else cfg.ledger
    out = []
    for hh, mm in schedule.as_tuples(schedule.times(raw, market)):
        slot = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if now < slot + timedelta(minutes=GRACE_MIN):
            continue
        a = (slot - timedelta(minutes=3)).astimezone(KST).isoformat(timespec="seconds")
        b = (slot + timedelta(minutes=GRACE_MIN)).astimezone(KST).isoformat(timespec="seconds")
        row = j.db.execute("SELECT 1 FROM run_logs WHERE mode=? AND kind='trade' AND ts BETWEEN ? AND ? LIMIT 1",
                           (ledger, a, b)).fetchone()
        if row is None:
            out.append(f"{hh:02d}:{mm:02d}")
    return out


def check(j, cfg, raw: dict, notify_fn=None, now: datetime | None = None) -> list[str]:
    """누락을 찾아 (처음 발견한 것만) 알림. 반환: 이번에 알린 메시지들."""
    sent = []
    for mk in ["kr"] + (["us"] if cfg.us_enabled else []):
        if not timer_installed(mk):
            continue
        tz = ZoneInfo(schedule.TZ[mk])
        day = (now or datetime.now(KST)).astimezone(tz).date().isoformat()
        for slot in missed_slots(j, cfg, raw, mk, now):
            key = f"missed_{mk}_{day}_{slot}"
            if j.get(key):
                continue
            j.set(key, "1")
            name = "🇰🇷 국내" if mk == "kr" else "🇺🇸 미국(뉴욕시간)"
            msg = (f"⏰ {name} {slot} 자동 실행이 기록되지 않았어요.\n"
                   f"서버·타이머 상태를 확인하세요: systemctl status {schedule.TIMER[mk]}")
            sent.append(msg)
            if notify_fn:
                notify_fn(cfg, "errors", msg)
    return sent


DISK_WARN_PCT = 85


def disk_check(j, cfg, notify_fn=None, now: datetime | None = None, usage=None) -> str | None:
    """디스크가 거의 찼으면(85% 이상) 하루 한 번 알림 — 가득 차면 일지 기록·백업·주문 기록이 모두 실패한다.
    루트(/)와 data 폴더가 있는 디스크를 본다. 반환: 보낸 메시지 (없으면 None)."""
    import shutil
    usage = usage or shutil.disk_usage
    worst = None
    for path in dict.fromkeys(["/", str(cfg.data_dir)]):
        try:
            u = usage(path)
        except OSError:
            continue
        pct = u.used / u.total * 100 if u.total else 0
        if pct >= DISK_WARN_PCT and (worst is None or pct > worst[1]):
            worst = (path, pct, u.free)
    if worst is None:
        return None
    day = (now or datetime.now(KST)).date().isoformat()
    if j.get("disk_warn") == day:
        return None
    j.set("disk_warn", day)
    path, pct, free = worst
    msg = (f"💽 서버 디스크가 거의 찼어요 — {path} {pct:.0f}% 사용 (남은 공간 {free / 1e9:.1f}GB)\n"
           f"가득 차면 매매 기록·백업이 실패해요. 확인: du -xh --max-depth=1 /var | sort -h | tail\n"
           f"로그 정리: journalctl --vacuum-size=100M · truncate -s 0 /var/log/syslog")
    if notify_fn:
        notify_fn(cfg, "errors", msg)
    return msg


def ping(cfg, ok: bool = True) -> None:
    """자동 실행이 끝날 때 외부 헬스체크 주소에 신호 (없으면 아무것도 안 함)."""
    url = os.environ.get("HEALTHCHECK_URL_US" if cfg.market == "us" else "HEALTHCHECK_URL") \
        or (os.environ.get("HEALTHCHECK_URL") if cfg.market == "us" else None)
    if not url:
        return
    try:
        import requests
        requests.get(url.rstrip("/") + ("" if ok else "/fail"), timeout=5)
    except Exception as e:  # noqa: BLE001
        log.warning("헬스체크 신호 실패: %s", e)
