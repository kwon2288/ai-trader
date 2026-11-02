"""액면분할·병합 대비 — 봇 장부가 분할 전 평단으로 남아 '-90% 손실'로 착각해 손절하는 일을 막는다.

매 실행 때 계좌의 종목별 총수량·현재가를 기억해 두고(state: seen_<시장>_<종목>), 다음 실행과 비교한다.
  - 수량이 정수배(2배·3배… 또는 1/2·1/3…)로 바뀌었고, 가격이 그 반대 비율로 움직였고, 그 사이 봇 주문이 없으면
    → 분할(병합)로 보고 봇 장부의 수량·평단을 같은 비율로 고친다. 알림.
  - 수량은 그대로인데 가격만 정수배로 뛰었으면(증권사가 수량을 하루 이틀 늦게 반영하는 경우)
    → 그 종목은 매매를 멈춘다(손절·익절·매수 모두). 수량이 반영되면 위에서 고쳐지고 풀린다. 7일 지나면 확인 요청 후 풀림.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

from .journal import now

PAUSE_DAYS = 7


def _ratio(x: float, tol: float) -> float | None:
    """x 가 정수배(≥2) 또는 1/정수(≤1/2)에 가까우면 그 비율, 아니면 None."""
    if not x or x <= 0:
        return None
    if x >= 1.8:
        n = round(x)
        return float(n) if abs(x / n - 1) <= tol else None
    if x <= 0.56:
        n = round(1 / x)
        return 1.0 / n if abs((1 / x) / n - 1) <= tol else None
    return None


def _traded_since(j, code: str, ts: str) -> bool:
    row = j.db.execute("SELECT 1 FROM orders WHERE code=? AND ok=1 AND ts > ? LIMIT 1", (code, ts)).fetchone()
    return row is not None


def _adjust_lot(j, code: str, r: float) -> tuple | None:
    row = j.db.execute("SELECT qty, avg FROM bot_positions WHERE code=?", (code,)).fetchone()
    if row is None or row["qty"] is None:
        return None
    q0, a0 = row["qty"], row["avg"] or 0
    q1 = math.floor(q0 * r + 1e-9)
    a1 = round(a0 / r, 4) if a0 else a0
    if q1 < 1:
        j.close_position(code)
    else:
        j.db.execute("UPDATE bot_positions SET qty=?, avg=? WHERE code=?", (q1, a1, code))
        j.db.commit()
    return q0, q1, a0, a1


def check(j, cfg, holdings: dict, say=None, notify=None) -> set[str]:
    """계좌 보유(holdings: 증권사 원본)를 보고 분할을 반영하고, 매매를 멈출 종목 코드를 돌려준다."""
    paused: set[str] = set()
    lots = j.bot_lots(cfg.market, cfg.ledger)
    t_now = now()
    for code, (bq, _ba) in lots.items():
        h = holdings.get(code)
        if h is None or not h.qty or not h.now_price:
            continue
        key = f"seen_{cfg.market}_{code}"
        seen = (j.get(key) or "").split("|")
        save = f"{h.qty}|{h.now_price}|{t_now.isoformat(timespec='seconds')}"
        if len(seen) != 3:
            j.set(key, save)
            continue
        q0, p0, ts0 = int(float(seen[0])), float(seen[1]), seen[2]
        if h.qty != q0 and q0 > 0 and bq and not _traded_since(j, code, ts0):
            r = _ratio(h.qty / q0, 0.05)
            if r and abs((p0 / h.now_price) / r - 1) <= 0.25:
                res = _adjust_lot(j, code, r)
                if res:
                    kind = "분할" if r > 1 else "병합"
                    msg = (f"🔀 {h.name}({code}) 주식 {kind} 감지 (수량 {q0}→{h.qty}주, 가격 {p0:g}→{h.now_price:g}) — "
                           f"봇 장부를 고쳤어요: {res[0]}주 → {res[1]}주, 평단 {res[2]:g} → {res[3]:g}")
                    if say:
                        say(msg)
                    if notify:
                        notify("orders", msg)
                j.set(key, save)
                j.set(f"split_pause_{cfg.market}_{code}", "")
                continue
        if h.qty == q0:
            k = _ratio(p0 / h.now_price, 0.10)
            if k:
                since = j.get(f"split_pause_{cfg.market}_{code}") or ""
                if not since:
                    j.set(f"split_pause_{cfg.market}_{code}", t_now.isoformat(timespec="seconds"))
                    msg = (f"⏸ {h.name}({code}) 가격이 {p0:g} → {h.now_price:g} 로 정수배 변했는데 수량은 그대로예요 — "
                           f"액면{'분할' if k > 1 else '병합'}이 아직 계좌에 반영되지 않은 것 같아 이 종목은 매매를 멈춰요 "
                           f"(손절 포함). 수량이 반영되면 자동으로 장부를 고치고 다시 매매해요.")
                    if notify:
                        notify("halt", msg)
                    if say:
                        say(msg)
                elif t_now - datetime.fromisoformat(since) > timedelta(days=PAUSE_DAYS):
                    msg = (f"⚠ {h.name}({code}) {PAUSE_DAYS}일째 분할 반영이 없어 매매 멈춤을 풀어요 — "
                           f"실제 급등락이었는지 계좌를 확인하세요.")
                    if notify:
                        notify("halt", msg)
                    if say:
                        say(msg)
                    j.set(f"split_pause_{cfg.market}_{code}", "")
                    j.set(key, save)
                    continue
                else:
                    if say:
                        say(f"⏸ {h.name}({code}) 분할 반영 대기 중 — 매매 멈춤")
                paused.add(code)
                continue                                  # 기준 가격은 분할 전 그대로 둔다
        j.set(key, save)
    return paused
