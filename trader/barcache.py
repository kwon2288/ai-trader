"""일봉 하루 캐시 — 과거 일봉은 그날 첫 실행 때 한 번만 받고, 그다음 실행부터는 오늘 시세로 오늘 봉만 갈아 끼운다.

- 증권사가 시세에 오늘 시가·고가·저가·거래량을 주고(NH 국내·미국), 지금 정규장일 때만 캐시를 쓴다.
  그 밖(장 시작 전·휴장·정보 없음)에는 지금처럼 매번 일봉을 새로 받는다.
- 하루 지나면(그 시장 날짜 기준) 다시 받는다. 파일: data/bars_daily/<시장>/<종목>.json
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = {"kr": "Asia/Seoul", "us": "America/New_York"}


def market_day(market: str) -> str:
    return datetime.now(ZoneInfo(TZ.get(market, "Asia/Seoul"))).strftime("%Y%m%d")


def _path(data_dir, market: str, code: str) -> Path:
    return Path(data_dir) / "bars_daily" / market / f"{code.replace('/', '_')}.json"


def today_bar(q, day: str) -> dict | None:
    if not (q.day_volume and q.day_high and q.day_low and q.price):
        return None
    return {"date": day, "open": q.day_open or q.price, "high": max(q.day_high, q.price),
            "low": min(q.day_low, q.price), "close": q.price, "volume": q.day_volume}


def daily(broker, code: str, q, data_dir, market: str, n: int = 80, stats: dict | None = None) -> list[dict]:
    """일봉 (오래된→최신). q: 방금 받은 시세."""
    day = market_day(market)
    f = _path(data_dir, market, code)
    tb = today_bar(q, day) if q is not None and q.market_status == "1" else None
    if tb:
        try:
            d = json.loads(f.read_text())
            if d.get("day") == day and len(d.get("hist") or []) >= n - 5:
                if stats is not None:
                    stats["hit"] = stats.get("hit", 0) + 1
                return d["hist"][-(n - 1):] + [tb]
        except (OSError, ValueError):
            pass
    bars = broker.daily(code, n)
    if stats is not None:
        stats["miss"] = stats.get("miss", 0) + 1
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"day": day, "hist": [b for b in bars if b["date"] < day]}))
    except OSError:
        pass
    return bars
