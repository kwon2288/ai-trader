"""체결 확인 — 보낸 주문이 실제로 얼마나·얼마에 체결됐는지 증권사에 물어 장부(봇 보유 수량·평단)를 맞춘다.

- 주문을 보내면 orders.status='sent'. 봇 보유 수량은 '체결된 만큼만' 늘리거나 줄인다.
- 매 실행 시작 때(그리고 장중 감시 때) 대기 중인 주문을 확인하고,
  지난 실행에서 남은 미체결 주문은 취소한다 (가격이 오래돼서).
- 증권사가 체결 조회를 지원하지 않으면(테스트용 가짜 증권사 등) 주문 즉시 체결된 것으로 본다.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime

from .journal import KST

log = logging.getLogger("fills")


def supported(broker) -> bool:
    return hasattr(broker, "order_status") and hasattr(broker, "cancel")


def _apply(j, cfg, o: dict, delta: float, price: float) -> None:
    if delta <= 0:
        return
    if o["side"] == "BUY":
        j.open_position(o["code"], o["name"], cfg.market, int(delta), price)
    else:
        j.reduce_position(o["code"], int(delta))


def reconcile(j, cfg, broker, say=print, cancel_before: str | None = None, notify=None) -> int:
    """대기 중인 주문을 확인. cancel_before(ISO 시각)보다 먼저 낸 미체결 주문은 취소. 반환: 처리한 주문 수"""
    if not supported(broker) or cfg.mode == "dry":
        return 0
    today = datetime.now(KST).date().isoformat()
    n = 0
    for o in j.open_orders(cfg.ledger):
        day = o["ts"][:10]
        try:
            f = broker.order_status(o["order_no"], o["code"], o["side"], o["qty"], o["est_price"], day)
        except Exception as e:  # noqa: BLE001
            say(f"⚠ 체결 확인 실패 ({o['name']}): {e}")
            continue
        prev = float(o.get("filled_qty") or 0)
        if f is None:
            if day < today:     # 지난 주문인데 조회도 안 됨 → 다 체결된 것으로 간주 (기록 보존)
                _apply(j, cfg, o, o["qty"] - prev, o["est_price"])
                j.update_fill(o["rowid"], "assumed", o["qty"], o["est_price"])
                say(f"ℹ {o['name']} 주문({day})을 증권사에서 찾지 못해 체결된 것으로 처리했어요")
            continue
        _apply(j, cfg, o, f.filled - prev, f.avg_price or o["est_price"])
        state = f.state
        if not f.done and (day < today or (cancel_before and o["ts"] < cancel_before)):
            if day < today:
                state = "canceled"          # 당일 주문은 장 마감으로 이미 소멸
            else:
                r = broker.cancel(o["order_no"], o["code"])
                if r.ok:
                    time.sleep(1)
                    try:
                        f2 = broker.order_status(o["order_no"], o["code"], o["side"], o["qty"], o["est_price"], day)
                    except Exception:  # noqa: BLE001
                        f2 = None
                    if f2 is not None:
                        _apply(j, cfg, o, f2.filled - f.filled, f2.avg_price or f.avg_price or o["est_price"])
                        f = f2
                    state = "canceled" if not (f2 and f2.state == "filled") else "filled"
                else:
                    say(f"⚠ {o['name']} 미체결 주문 취소 실패: {r.message}")
            if state == "canceled":
                side = "매수" if o["side"] == "BUY" else "매도"
                msg = (f"↩ {o['name']} {side} 미체결 정리: {o['qty']}주 중 {int(f.filled)}주 체결, "
                       f"나머지 {o['qty'] - int(f.filled)}주 취소")
                say(msg)
                if notify:
                    notify("orders", msg)
        j.update_fill(o["rowid"], state, f.filled, f.avg_price or None, f.fee, f.tax)
        if f.filled > prev and state != "canceled":
            side = "매수" if o["side"] == "BUY" else "매도"
            say(f"✔ 체결 확인: {o['name']} {side} {int(f.filled)}/{o['qty']}주 @ {cfg.money(f.avg_price or o['est_price'])}")
        n += 1
    return n
