"""체결 확인 — 보낸 주문이 실제로 얼마나·얼마에 체결됐는지 증권사에 물어 장부(봇 보유 수량·평단)를 맞춘다.

- 주문을 보내면 orders.status='sent'. 봇 보유 수량은 '체결된 만큼만' 늘리거나 줄인다.
- 매 실행 시작 때(그리고 장중 감시 때) 대기 중인 주문을 확인하고,
  지난 실행에서 남은 미체결 주문은 취소한다 (가격이 오래돼서).
- 증권사가 체결 조회를 지원하지 않으면(테스트용 가짜 증권사 등) 주문 즉시 체결된 것으로 본다.
"""
from __future__ import annotations

import json
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


# ── 응답 없는 주문 (시간 초과 등) ───────────────────────────
# 주문 요청이 시간 초과·네트워크 오류로 끝나도 증권사에는 접수됐을 수 있다. 이때 주문을 다시 보내면 중복 매매,
# 그냥 잊으면 계좌엔 있는데 봇은 모르는 ‘고아’ 종목(손절 감시 밖)이 생긴다. 그래서
#   1) 주문 전 계좌 수량과 함께 ‘확인 필요’로 남기고, 그 종목은 확인될 때까지 매매하지 않는다
#   2) 다음 실행·장중 감시 때 계좌 수량을 보고: 늘었으면(매수)/줄었으면(매도) 체결된 것으로 장부에 반영,
#      그대로인데 그 거래일이 지났으면 접수 안 된 것으로 정리 — 둘 다 알림.

def _unsure_key(cfg) -> str:
    return f"unsure_{cfg.ledger}"


def unsure_list(j, cfg) -> list[dict]:
    try:
        return json.loads(j.get(_unsure_key(cfg)) or "[]")
    except ValueError:
        return []


def mark_unsure(j, cfg, side: str, code: str, name: str, qty: int, est_price: float, trigger: str,
                before_qty: int, error: str) -> None:
    from .journal import now
    items = unsure_list(j, cfg)
    items.append({"ts": now().isoformat(timespec="seconds"), "day": market_today(cfg.market), "side": side,
                  "code": code, "name": name, "qty": int(qty), "price": float(est_price), "trigger": trigger,
                  "before": int(before_qty), "error": str(error)[:200]})
    j.set(_unsure_key(cfg), json.dumps(items, ensure_ascii=False))


def resolve_unsure(j, cfg, holdings: dict, say=print, notify=None, broker=None) -> set[str]:
    """응답 없던 주문 확인. 반환: 아직 확인 못 한 종목(이번에 매매하지 않을 것).
    ① 증권사가 주문번호 없이 주문을 찾을 수 있으면(NH 국내 find_orders): 그날 같은 종목·방향·수량 주문 중
       봇이 이미 아는 주문을 뺀 것이 딱 1건이면 그 주문번호로 장부에 넣고(체결 수량·가격은 체결 확인이 증권사 값으로),
       2건 이상이면 어느 게 봇 주문인지 몰라 확정하지 않고 알림.
    ② 못 찾는 증권사: 계좌 수량 변화가 주문 수량과 정확히 같을 때만 ‘추정 체결’로 넣고 알림.
       다르면(사용자가 같은 종목을 따로 사고팔았을 수 있음) 확정하지 않고 직접 확인해 달라고 알림."""
    from .broker import OrderResult
    items = unsure_list(j, cfg)
    if not items:
        return set()
    keep, pending = [], set()
    today = market_today(cfg.market)
    finder = getattr(broker, "find_orders", None) if broker is not None else None

    def tell(msg: str) -> None:
        say(msg)
        if notify:
            notify("orders", msg)

    for it in items:
        side_k = "매수" if it["side"] == "BUY" else "매도"
        label = f"{it['name']}({it['code']}) {side_k} {it['qty']}주"
        passed = it["day"] < today
        if finder:
            try:
                found = finder(it["code"], it["side"], it["qty"], it["day"])
            except Exception as e:  # noqa: BLE001
                say(f"⚠ 응답 없던 주문 조회 실패 ({label}): {e}")
                keep.append(it)
                pending.add(it["code"])
                continue
            known = {str(r[0]).lstrip("0") for r in j.db.execute(
                "SELECT order_no FROM orders WHERE mode=? AND code=? AND order_no IS NOT NULL", (cfg.ledger, it["code"]))}
            found = [(no, f) for no, f in found if str(no).lstrip("0") not in known]
            if len(found) == 1:
                no, f = found[0]
                if f.state == "rejected" or (f.state == "canceled" and not f.filled):
                    tell(f"ℹ 응답이 없던 {label} 주문은 증권사에서 거부·취소된 것으로 확인했어요 (주문번호 {no})")
                    continue
                j.add_order(cfg.ledger, it["side"], it["code"], it["name"], it["qty"], it["price"],
                            OrderResult(True, no, f"응답 없던 주문 — 증권사 주문 내역에서 확인 ({it['error'][:80]})"),
                            f"{it['trigger']} (응답 없음 → 증권사 확인)", status=None)
                tell(f"✅ 응답이 없던 {label} 주문을 증권사 주문 내역에서 찾았어요 (주문번호 {no}, 지금 체결 "
                     f"{f.filled:g}주) — 체결 수량·가격은 증권사 값으로 장부에 반영해요")
                pending.add(it["code"])            # 이번 실행은 쉬고, 체결 확인이 장부를 맞춘 뒤부터 매매
                continue
            if len(found) > 1:
                if not it.get("told"):
                    tell(f"⚠ 응답이 없던 {label}: 같은 날 같은 조건 주문이 {len(found)}건이라 어느 게 봇 주문인지 몰라요. "
                         f"증권사 앱에서 확인해 주세요 — 확인 전까지 이 종목은 매매하지 않아요")
                    it["told"] = True
                if passed:
                    tell(f"⚠ {label}: 끝내 확정하지 못했어요. 봇 장부엔 넣지 않아서 봇은 이 수량을 직접 보유분으로 보고 건드리지 않아요 — 필요하면 증권사 앱에서 직접 정리해 주세요")
                    continue
                keep.append(it)
                pending.add(it["code"])
                continue
            if passed:
                tell(f"ℹ 응답이 없던 {label} 주문은 증권사 주문 내역에 없어 접수되지 않은 것으로 확인했어요")
                continue
            keep.append(it)
            pending.add(it["code"])
            continue
        # ② 주문 조회가 안 되는 증권사 — 계좌 수량 변화로 추정
        h = holdings.get(it["code"])
        now_q = int(h.qty) if h else 0
        moved = now_q - it["before"] if it["side"] == "BUY" else it["before"] - now_q
        if moved == it["qty"]:
            q, px = it["qty"], it["price"]
            j.add_order(cfg.ledger, it["side"], it["code"], it["name"], q, px,
                        OrderResult(True, "ESTIMATED", f"응답 없던 주문 — 계좌 수량 변화로 추정 ({it['error'][:80]})"),
                        f"{it['trigger']} (응답 없음 → 수량으로 추정)", status="filled")
            if it["side"] == "BUY":
                j.open_position(it["code"], it["name"], cfg.market, q, px)
            else:
                j.reduce_position(it["code"], q)
            tell(f"⚠ 응답이 없던 {label} 주문이 계좌 수량으로 보아 체결된 것 같아요 → 봇 장부에 ‘추정 체결’로 넣었어요 "
                 f"(가격은 주문 때 예상가 {cfg.money(px)}, 증권사 앱에서 실제 체결가를 확인하세요)")
        elif moved > 0:
            if not it.get("told"):
                tell(f"⚠ 응답이 없던 {label}: 계좌 수량이 {moved}주 바뀌어 주문 수량과 달라요 — 직접 사고판 게 섞였을 수 있어 "
                     f"확정하지 않았어요. 증권사 앱에서 확인해 주세요")
                it["told"] = True
            if passed:
                tell(f"⚠ {label}: 끝내 확정하지 못했어요. 봇 장부엔 넣지 않아서 봇은 이 수량을 직접 보유분으로 보고 건드리지 않아요 — 필요하면 증권사 앱에서 직접 정리해 주세요")
                continue
            keep.append(it)
            pending.add(it["code"])
        elif passed:
            tell(f"ℹ 응답이 없던 {label} 주문은 접수되지 않은 것으로 확인했어요 (계좌 수량 그대로)")
        else:
            keep.append(it)
            pending.add(it["code"])
    j.set(_unsure_key(cfg), json.dumps(keep, ensure_ascii=False))
    if pending:
        say("⏳ 응답 없던 주문 확인 중: " + ", ".join(sorted(pending)) + " — 확인될 때까지 이 종목은 매매하지 않아요")
    return pending
