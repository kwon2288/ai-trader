"""로그인 실패 차단 — 인터넷에 열린 화면이라 비밀번호 무차별 대입을 막는다.

- 같은 곳(IP)에서 5번 틀리면 15분 차단
- 어디서든 합쳐 15분 안에 20번 틀리면 15분 동안 비밀번호 로그인 전체 잠금 (IP 를 바꿔 가며 시도하는 경우)
  → 이때도 등록한 패스키로는 로그인 가능
- 차단될 때마다 알림(오류 알림을 켜 둔 경우)
메모리에만 둔다 (웹을 다시 시작하면 초기화).
"""
from __future__ import annotations

import threading
import time

PER_IP = 5
GLOBAL = 20
WINDOW = 15 * 60
LOCK = 15 * 60

_lock = threading.Lock()
_fails: dict[str, list[float]] = {}      # ip → 실패 시각들
_all: list[float] = []
_blocked: dict[str, float] = {}          # ip → 풀리는 시각 ("*" = 전체)


def client_ip(request) -> str:
    """프록시 뒤라면 프록시가 마지막에 붙인 주소(X-Forwarded-For 의 맨 끝)를 쓴다 — 앞쪽은 접속자가 꾸밀 수 있음."""
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[-1].strip()
    return request.headers.get("X-Real-IP") or request.remote_addr or "?"


def blocked(ip: str, now: float | None = None) -> int:
    """차단 중이면 남은 초, 아니면 0."""
    now = now or time.time()
    with _lock:
        left = max(_blocked.get(ip, 0), _blocked.get("*", 0)) - now
    return int(left) if left > 0 else 0


def fail(ip: str, now: float | None = None) -> str | None:
    """실패 기록. 이번에 새로 차단되면 알림 문구를 돌려준다."""
    now = now or time.time()
    with _lock:
        lst = [t for t in _fails.get(ip, []) if now - t < WINDOW] + [now]
        _fails[ip] = lst
        _all[:] = [t for t in _all if now - t < WINDOW] + [now]
        if len(_all) >= GLOBAL and _blocked.get("*", 0) <= now:
            _blocked["*"] = now + LOCK
            _all.clear()
            return f"🚨 웹 로그인 실패가 15분 안에 {GLOBAL}번 — 비밀번호 로그인을 15분 동안 잠갔어요 (패스키는 가능). 마지막 시도: {ip}"
        if len(lst) >= PER_IP and _blocked.get(ip, 0) <= now:
            _blocked[ip] = now + LOCK
            _fails[ip] = []
            return f"🚨 웹 로그인 비밀번호가 {PER_IP}번 틀려 {ip} 를 15분 차단했어요. 본인이 아니면 비밀번호를 바꾸세요."
    return None


def success(ip: str) -> None:
    with _lock:
        _fails.pop(ip, None)


def reset() -> None:
    with _lock:
        _fails.clear()
        _all.clear()
        _blocked.clear()
