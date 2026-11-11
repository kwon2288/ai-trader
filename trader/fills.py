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


def session_day(ts: str, market: str) -> str:
    """주문 시각(한국시간 ISO) → 그 시장의 거래일. 미국은 뉴욕 날짜 (한국 23:40 주문과 다음 날 04:00 은 같은 장)."""
    if market != "us":
        return ts[:10]
    from zoneinfo import ZoneInfo
    return datetime.fromisoformat(ts).astimezone(ZoneInfo("America/New_York")).date().isoformat()


def market_today(market: str) -> str:
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("America/New_York") if market == "us" else KST
    return datetime.now(tz).date().isoformat()


def reconcile(j, cfg, broker, say=print, cancel_before: str | None = None, notify=None) -> int:
    """대기 중인 주문을 확인. cancel_before(ISO 시각)보다 먼저 낸 미체결 주문은 취소. 반환: 처리한 주문 수"""
    if not supported(broker) or cfg.mode == "dry":
        return 0
    today = market_today(cfg.market)
    n = 0
    for o in j.open_orders(cfg.ledger):
        day = session_day(o["ts"], cfg.market)       # 그 시장 거래일 (미국은 뉴욕 날짜)
        try:
            f = broker.order_status(o["order_no"], o["code"], o["side"], o["qty"], o["est_price"], day)
        except Exception as e:  # noqa: BLE001
            say(f"⚠ 체결 확인 실패 ({o['name']}): {e}")
            continue
        prev = float(o.get("filled_qty") or 0)
        if f is None:
            if day < today:
                if o["side"] == "SELL":
                    # 지난 매도인데 조회가 안 됨 → 판 것으로 (봇이 그 종목 관리를 멈추는 쪽이라 안전)
                    _apply(j, cfg, o, o["qty"] - prev, o["est_price"])
                    j.update_fill(o["rowid"], "assumed", o["qty"], o["est_price"])
                    say(f"ℹ {o['name']} 매도 주문({day})을 증권사에서 찾지 못해 체결된 것으로 처리했어요")
                else:
                    # 매수를 체결로 가정하면 실제로 없는 주식을 봇 몫으로 잡아 사용자 주식을 팔 수 있다 → 가정하지 않음
                    j.update_fill(o["rowid"], "unknown", prev, None)
                    msg = (f"⚠ {o['name']} 매수 주문({day})을 증권사에서 찾지 못했어요 — 체결 안 된 것으로 보고 "
                           f"봇 장부에 넣지 않았어요. 실제로 샀다면 계좌에서 확인해 주세요.")
                    say(msg)
                    if notify:
                        notify("orders", msg)
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


def sync_settled_fx(j, cfg, broker, say=print) -> int:
    """미국 주문의 환율을 증권사 결제 환율로 확정 (토스). 주문 때는 참고 환율을 남기고, 결제(보통 1영업일 뒤)가 끝나면
    거래내역의 실제 환율로 바꾼다. 확정할 주문이 없으면 조회하지 않는다. 반환: 확정한 주문 수"""
    if cfg.market != "us" or cfg.mode == "dry" or not hasattr(broker, "settled_fx"):
        return 0
    from datetime import date, timedelta
    since = (datetime.now(KST) - timedelta(days=21)).isoformat(timespec="seconds")
    rows = [dict(r) for r in j.db.execute(
        "SELECT rowid, ts, side, code, filled_qty FROM orders WHERE mode=? AND ok=1 AND COALESCE(fx_settled,0)=0 "
        "AND COALESCE(filled_qty,0) > 0 AND COALESCE(order_no,'') != 'DRY' AND ts >= ? ORDER BY ts DESC",
        (cfg.ledger, since))]
    if not rows:
        return 0
    try:
        txs = broker.settled_fx()
    except Exception as e:  # noqa: BLE001
        say(f"ℹ 결제 환율 조회 실패 (다음 실행 때 다시 확인): {e}")
        return 0
    n = 0
    for o in rows:                                      # 최신 주문부터, 거래내역도 최신순 → 같은 조건이면 가까운 것끼리
        day = date.fromisoformat(session_day(o["ts"], "us"))
        for t in txs:
            try:
                sd = date.fromisoformat(t["date"])
            except ValueError:
                continue
            if (t["code"] == o["code"] and t["side"] == o["side"] and abs(t["qty"] - float(o["filled_qty"])) < 1e-6
                    and day <= sd <= day + timedelta(days=6)):
                j.db.execute("UPDATE orders SET fx=?, fx_settled=1 WHERE rowid=?", (t["fx"], o["rowid"]))
                txs.remove(t)
                n += 1
                break
    if n:
        j.db.commit()
        say(f"💱 미국 주문 {n}건의 환율을 결제 환율로 확정했어요")
    return n
