"""지수 비교 — 봇 손익을 같은 기간 지수(국내 KODEX 200, 미국 SPY)와 나란히 보기.

정기 실행이 하루 한 번 지수 일봉을 받아 journal 에 저장해 두고(refresh),
손익 화면·주간 리포트는 저장된 값만 읽는다(change) — 화면을 열 때 증권사를 부르지 않게.
"""
from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

from .strategy import BENCH

log = logging.getLogger(__name__)
DAYS = 260          # 약 1년치


def _key(market: str) -> str:
    return f"bench_{market}"


def refresh(cfg, broker, j, force: bool = False) -> bool:
    """지수 종가를 저장 (하루 한 번, force 면 다시). 실패해도 매매에는 영향 없음."""
    from . import backtest
    from .journal import now
    today = now().date().isoformat()
    old = load(j, cfg.market)
    if old and old.get("day") == today and not force:
        return False
    code, name = BENCH[cfg.market]
    try:
        bars = backtest.load_bars(broker, code, DAYS, Path(cfg.data_dir) / "bars" / cfg.market)
    except Exception as e:  # noqa: BLE001
        log.warning("지수(%s) 받기 실패: %s", code, e)
        return False
    closes = [[b["date"], float(b["close"])] for b in bars if b.get("close")]
    if not closes:
        return False
    j.set(_key(cfg.market), json.dumps({"day": today, "code": code, "name": name, "closes": closes}))
    return True


def load(j, market: str) -> dict | None:
    try:
        return json.loads(j.get(_key(market)) or "null")
    except ValueError:
        return None


def _ymd(d) -> str:
    return d.strftime("%Y%m%d") if isinstance(d, date) else str(d).replace("-", "")[:8]


def change(j, market: str, start, end=None) -> dict | None:
    """start~end 기간 지수 등락률. 기준 = start 전 마지막 종가(그날 움직임까지 포함), 끝 = end 까지의 마지막 종가.
    반환 {name, code, pct, base_date, end_date} — 데이터가 없거나 기간이 데이터 밖이면 None."""
    b = load(j, market)
    if not b or not b.get("closes") or start is None:
        return None
    s, e = _ymd(start), _ymd(end) if end else "99999999"
    before = [c for d, c in b["closes"] if d < s]
    upto = [(d, c) for d, c in b["closes"] if d <= e]
    if not before or not upto:
        return None
    base_d = max(d for d, _ in b["closes"] if d < s)
    end_d, last = upto[-1]
    if end_d < s:                       # 그 기간에 아직 거래일이 없음 (예: 오늘 장 전)
        return {"name": b["name"], "code": b["code"], "pct": 0.0, "base_date": base_d, "end_date": base_d}
    return {"name": b["name"], "code": b["code"], "pct": round((last / before[-1] - 1) * 100, 2),
            "base_date": base_d, "end_date": end_d}


def line(j, market: str, start, end, bot_pct: float | None, money=None, budget: float | None = None) -> str | None:
    """알림용 한 줄: ‘지수 비교: KODEX 200 +2.1% · 봇 -0.3% → 지수가 2.4%p 앞섬’."""
    c = change(j, market, start, end)
    if not c:
        return None
    s = f"{c['name'].split(' (')[0]} {c['pct']:+.2f}%"
    if bot_pct is None:
        return f"같은 기간 지수: {s}"
    gap = bot_pct - c["pct"]
    who = "봇이" if gap > 0 else "지수가"
    tail = f" → {who} {abs(gap):.2f}%p 앞섬" if abs(gap) >= 0.01 else " → 같음"
    if money and budget:
        tail += f" (예산을 지수에 넣었다면 {money(budget * c['pct'] / 100)})"
    return f"지수 비교: {s} · 봇 {bot_pct:+.2f}%{tail}"


def bot_change(j, ledger: str, budget: float, start, end=None) -> float | None:
    """같은 기간 봇 자산(실현 + 평가) 변화를 예산 대비 % 로. 정기 실행 기록(runs.equity) 기준."""
    series = j.equity_series(ledger, 5000)
    if not series or not budget:
        return None
    s = start.isoformat() if hasattr(start, "isoformat") else str(start)
    e = (end.isoformat() if hasattr(end, "isoformat") else str(end)) if end else "9999"
    before = [x for x in series if x["ts"][:10] < s]
    inside = [x for x in series if s <= x["ts"][:10] <= e]
    if not inside:
        return None
    e0 = before[-1]["equity"] if before else inside[0]["equity"]
    return round((inside[-1]["equity"] - e0) / float(budget) * 100, 2)
