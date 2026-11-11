"""봇 손익 계산 — 예산 고정 방식.

- 설정 예산(원금)은 봇이 '동시에 투자할 수 있는 최대 금액'. 수익은 재투자하지 않고 계좌에 남는다.
- 지금 살 수 있는 금액 = min(예산 - 봇 투자 중(매입금액), 계좌 주문가능 현금)
- 봇 손익 = 실현 손익(봇 매도로 확정, 누적) + 평가 손익(봇 보유분)
- 실현 손익은 주문 기록을 처음부터 다시 계산(평균단가법)한다. 수수료·세금은 빠진 추정치.
"""
from __future__ import annotations

import re
from datetime import date

# 수수료·세금 기본값 (%) — 웹 설정 → 예산·위험관리에서 바꿀 수 있음. 증권사가 실제 값을 주면 그걸 쓴다.
DEFAULT_COSTS = {"kr_fee_pct": 0.015, "kr_sell_tax_pct": 0.20, "us_fee_pct": 0.25}
# 국내 ETF·ETN 은 매도 거래세가 없다
_ETF = re.compile(r"^(KODEX|TIGER|KBSTAR|RISE|ACE|SOL|HANARO|KOSEF|ARIRANG|PLUS|KIWOOM|TIMEFOLIO|WON|BNK|1Q|파워|"
                  r"마이티|히어로즈|에셋플러스|UNICORN|VITA|ITF|TRUSTON|KoAct|마이다스|DAISHIN|FOCUS)\b|ETF|ETN", re.I)


def costs_of(cfg) -> dict:
    c = dict(DEFAULT_COSTS)
    c.update({k: float(v) for k, v in (getattr(cfg, "costs", None) or {}).items() if k in DEFAULT_COSTS})
    return c


def fee_tax(o: dict, qty: float, price: float, costs: dict, ledger: str) -> float:
    """주문 한 건의 수수료+세금. 증권사가 알려 준 값이 있으면 그것, 없으면 설정 비율로 추정."""
    if o.get("fee") is not None or o.get("tax") is not None:
        return float(o.get("fee") or 0) + float(o.get("tax") or 0)
    amt = qty * price
    if ledger.endswith("_us"):
        return amt * costs["us_fee_pct"] / 100
    tax = 0.0 if (o["side"] != "SELL" or _ETF.search(o.get("name") or "")) else amt * costs["kr_sell_tax_pct"] / 100
    return amt * costs["kr_fee_pct"] / 100 + tax


def _eff(o: dict) -> tuple[float, float]:
    """실제 체결 수량·가격. 체결 확인 기능 이전 기록(status 없음)은 주문 수량·예상가 그대로."""
    if o.get("status") is None or o.get("status") == "filled" and o.get("filled_qty") is None:
        return float(o["qty"]), float(o["est_price"])
    return float(o.get("filled_qty") or 0), float(o.get("fill_price") or o["est_price"])


def day_of(ts: str, ledger: str) -> str:
    """주문 시각(한국시간) → 그 시장 거래일. 미국 장부는 뉴욕 날짜 (한국 00:30 매도 = 뉴욕 전날 장)."""
    if not ledger.endswith("_us"):
        return ts[:10]
    from datetime import datetime
    from zoneinfo import ZoneInfo
    try:
        return datetime.fromisoformat(ts).astimezone(ZoneInfo("America/New_York")).date().isoformat()
    except ValueError:
        return ts[:10]


def _r(x: float, us: bool):
    """거래 한 건 금액 반올림 — 원화는 원 단위, 달러는 센트. 화면의 건별 금액과 합계가 1원도 어긋나지 않게."""
    return round(x, 2) if us else int(round(x))


def trades(j, ledger: str, costs: dict | None = None) -> list[dict]:
    """봇의 매도 체결마다 실현 손익 (주문 기록을 처음부터 평균단가로 계산, 수수료·세금 차감)."""
    costs = costs or DEFAULT_COSTS
    pos: dict[str, list] = {}           # code → [qty, cost(수수료 포함), 원화 원가(미국: 매수 당시 환율)]
    out = []
    us = ledger.endswith("_us")
    try:
        fx_now = float(j.get("fx_usdkrw") or 0) or 1400.0 if us else 1.0
    except ValueError:
        fx_now = 1400.0
    for o in j.db.execute(
            "SELECT * FROM orders WHERE ok=1 AND mode=? AND COALESCE(order_no,'') != 'DRY' "
            "AND COALESCE(status,'') != 'failed' ORDER BY ts, rowid", (ledger,)):
        o = dict(o)
        qty, px = _eff(o)
        if qty <= 0:
            continue
        ft = fee_tax(o, qty, px, costs, ledger)
        fx = (o.get("fx") or fx_now) if us else 1.0    # 환율 기록 이전 주문은 최근 환율로
        q, c, ck = pos.get(o["code"], [0, 0.0, 0.0])
        if o["side"] == "BUY":
            pos[o["code"]] = [q + qty, c + qty * px + ft, ck + (qty * px + ft) * fx]
            continue
        avg = c / q if q else px                       # 기록 없는 매도(예전 데이터)는 손익 0 으로
        avg_k = ck / q if q else px * fx
        sold = min(qty, q) if q else qty
        pnl = (px - avg) * sold - ft
        pnl_krw = (px * sold - ft) * fx - avg_k * sold    # 원화 손익 = 매도 당시 환율 원화 − 매수 당시 환율 원가 (환차손익 포함)
        out.append({"ts": o["ts"], "date": day_of(o["ts"], ledger), "fx": fx if us else None,
                    "pnl_krw": round(pnl_krw), "cost_krw": round(ft * fx), "code": o["code"], "name": o["name"], "qty": sold,
                    "price": px, "avg": round(avg, 4), "pnl": _r(pnl, us), "cost": _r(ft, us),
                    "pnl_pct": round(((px * sold - ft) / (avg * sold) - 1) * 100, 2) if avg else 0.0})
        nq = max(0, q - sold)
        pos[o["code"]] = [nq, avg * nq, avg_k * nq]
    return out


def realized_total(j, ledger: str, costs: dict | None = None) -> float:
    return round(sum(t["pnl"] for t in trades(j, ledger, costs)), 2)


def status(j, cfg, held: dict, account_cash: float | None) -> dict:
    """held: 봇 몫 보유 (수량·봇 평단·현재가). 코어-위성이면 코어 ETF 몫은 위성(매매) 예산에서 뺀다."""
    from . import strategy
    sg = strategy.settings(cfg)
    budget = float(cfg.total_krw)
    invested = sum(h.qty * h.avg_price for h in held.values())
    unrealized = sum(h.qty * (h.now_price - h.avg_price) for h in held.values())
    costs = costs_of(cfg)
    realized = realized_total(j, cfg.ledger, costs)
    pnl = realized + unrealized
    core = held.get(sg["core_code"]) if sg["core_pct"] else None
    core_inv = core.qty * core.avg_price if core else 0.0
    core_budget = budget * sg["core_pct"] / 100
    sat_budget = budget - core_budget
    # 다른 증권사에 남겨 둔 봇 종목은 매입금액만큼 예산을 차지한 것으로 본다 (돌아가면 다시 관리)
    parked = j.parked(cfg.market) if hasattr(j, "parked") else []
    parked_cost = sum((p["qty"] or 0) * (p["avg"] or 0) for p in parked)
    room = max(0.0, sat_budget - (invested - core_inv) - parked_cost)
    acash = None if account_cash is None or account_cash == float("inf") else float(account_cash)
    avail = room if (cfg.mode == "dry" or acash is None) else min(room, acash)
    shared = None
    if cfg.mode != "dry" and acash is not None:
        shared = shared_split(j, cfg, room, acash)
        if shared:
            avail = min(avail, shared["cap"])
    r = 2 if cfg.currency == "USD" else 0

    def rd(x):
        return round(x, r) if r else int(round(x))
    return {"budget": rd(budget), "invested": rd(invested), "value": rd(invested + unrealized),
            "unrealized": rd(unrealized), "realized": rd(realized), "pnl": rd(pnl),
            "pnl_pct": round(pnl / budget * 100, 2) if budget else 0.0, "equity": rd(budget + pnl),
            "room": rd(room), "avail": rd(avail), "account_cash": None if acash is None else rd(acash),
            "positions": len(held), "core_pct": sg["core_pct"], "core_code": sg["core_code"] if sg["core_pct"] else None,
            "core_budget": rd(core_budget), "core_invested": rd(core_inv),
            "core_value": rd(core.qty * core.now_price) if core else 0, "sat_budget": rd(sat_budget),
            "parked": [{"code": p["code"], "name": p["name"], "qty": p["qty"], "broker": _acct(p["broker"]),
                        "cost": rd((p["qty"] or 0) * (p["avg"] or 0))} for p in parked],
            "shared": shared}


def _acct(scope):
    from .bot import account_label
    return account_label(scope)


# ── 국내·미국 같은 계좌(증거금 공유) ────────────────────────────
def shared_account(cfg) -> bool:
    """국내·미국이 같은 계좌의 현금(원화 증거금)을 같이 쓰는지.
    config [us] shared_cash = "auto"(기본) | true | false.
    auto: 둘 다 NH 이고, 미국 계좌를 따로 지정하지 않았거나 같은 번호이고, 미국을 원화 증거금으로 살 때."""
    import os
    us = getattr(cfg, "us", None) or {}
    v = us.get("shared_cash", "auto")
    if isinstance(v, bool):
        return v and cfg.us_enabled
    v = str(v).strip().lower()
    if v in ("true", "on", "yes", "1"):
        return cfg.us_enabled
    if v in ("false", "off", "no", "0") or not cfg.us_enabled:
        return False
    from .runner import broker_name
    if broker_name(cfg.for_market("kr")) != "nh" or broker_name(cfg.for_market("us")) != "nh":
        return False
    a_kr = os.environ.get("NHPLUG_ACCOUNT", "").strip()
    a_us = os.environ.get("NHPLUG_ACCOUNT_US", "").strip()
    if a_us and a_kr and a_us != a_kr:
        return False
    return str(us.get("margin_currency", "krw")).strip().lower() == "krw"


def _room_from_journal(j, cfg) -> float:
    """다른 시장의 남은 예산 (봇 장부만으로 — 시세 조회 없이 매입금액 기준)."""
    from . import strategy
    sg = strategy.settings(cfg)
    saved, j.broker = j.broker, None            # 다른 시장은 증권사와 상관없이 봇 종목 전체
    try:
        lots = j.bot_lots(cfg.market, cfg.ledger)
    finally:
        j.broker = saved
    invested = core_inv = 0.0
    for code, (qty, avg) in lots.items():
        v = (qty or 0) * (avg or 0)
        invested += v
        if sg["core_pct"] and code == sg["core_code"]:
            core_inv += v
    sat = float(cfg.total_krw) * (1 - sg["core_pct"] / 100)
    return max(0.0, sat - (invested - core_inv))


def shared_split(j, cfg, room: float, acash: float) -> dict | None:
    """같은 계좌면 주문가능 현금을 두 시장의 남은 예산 비율로 나눈다 (현금이 넉넉하면 나누지 않음).
    반환: {cap(이 시장 통화), other_room(이 시장 통화), pct(이 시장 몫 %), short(현금 부족 여부), fx} 또는 None."""
    try:
        if not shared_account(cfg):
            return None
        fx = float(j.get("fx_usdkrw") or 0)
        other = "us" if cfg.market == "kr" else "kr"
        o_room = _room_from_journal(j, cfg.for_market(other))
    except Exception:  # noqa: BLE001
        return None
    if not fx:
        return {"cap": acash, "other_room": None, "pct": None, "short": False, "fx": None}
    o_conv = o_room * fx if cfg.market == "kr" else o_room / fx
    total = room + o_conv
    prio = priority(cfg)
    base = {"other_room": round(o_conv, 2), "fx": fx, "priority": prio}
    if total <= 0 or acash >= total:
        return {"cap": acash, "pct": None, "short": False, **base}
    if prio == "session":                        # 시간대별: 지금 실행 중인(장이 열린) 시장이 현금을 전부 씀
        cap = acash
    elif prio == cfg.market:                     # 이 시장 우선: 남은 예산만큼 먼저 씀
        cap = min(room, acash)
    elif prio in ("kr", "us"):                   # 다른 시장 우선: 그 시장 남은 예산을 먼저 떼어 둠
        cap = max(0.0, acash - o_conv)
    else:                                        # 균등(비율): 남은 예산 비율대로
        cap = acash * room / total
    pct = round(cap / acash * 100) if acash else 0
    return {"cap": round(cap, 2), "pct": pct, "short": True, **base}


PRIORITIES = {"split": "남은 예산 비율대로", "kr": "국내 우선", "us": "미국 우선",
              "session": "시간대별(낮 국내·밤 미국이 전부)"}


def priority(cfg) -> str:
    """config [us] shared_priority = "split"(기본) | "kr" | "us" | "session".
    session: 다른 시장 몫을 떼어 두지 않는다 — 낮(국내 장)엔 국내가, 밤(미국 장)엔 미국이 주문가능 현금을 전부 쓴다.
    두 시장 장 시간이 겹치지 않으니 실행 시점에 열린 시장이 곧 주인. 이미 산 종목이 쥔 현금은 팔 때까지 그 시장 것."""
    v = str((getattr(cfg, "us", None) or {}).get("shared_priority", "split")).strip().lower()
    return v if v in PRIORITIES else "split"


def period(j, ledger: str, start: date | None, end: date | None, costs: dict | None = None) -> dict:
    """기간 실현 손익 요약 (start~end 포함, None 이면 열린 범위)."""
    ts = [t for t in trades(j, ledger, costs)
          if (start is None or t["date"] >= start.isoformat()) and (end is None or t["date"] <= end.isoformat())]
    wins = [t for t in ts if t["pnl"] > 0]
    by_code: dict[str, dict] = {}
    for t in ts:
        b = by_code.setdefault(t["code"], {"code": t["code"], "name": t["name"], "pnl": 0.0, "n": 0})
        b["pnl"] += t["pnl"]
        b["n"] += 1
    by_day: dict[str, float] = {}
    for t in ts:
        by_day[t["date"]] = by_day.get(t["date"], 0.0) + t["pnl"]
    s_iso, e_iso = start and start.isoformat(), end and end.isoformat()
    buys = {"n": 0, "amt": 0.0, "amt_krw": 0.0}
    try:
        fx_now = float(j.get("fx_usdkrw") or 0) or 1400.0 if ledger.endswith("_us") else 1.0
    except ValueError:
        fx_now = 1400.0
    for o_ts, amt, o_fx in j.db.execute(
            "SELECT ts, COALESCE(filled_qty, qty)*COALESCE(fill_price, est_price), fx FROM orders "
            "WHERE ok=1 AND mode=? AND side='BUY' AND COALESCE(status,'') NOT IN ('failed') "
            "AND COALESCE(order_no,'') != 'DRY'", (ledger,)):
        d = day_of(o_ts, ledger)
        if (s_iso is None or d >= s_iso) and (e_iso is None or d <= e_iso):
            buys["n"] += 1
            buys["amt"] += amt or 0
            buys["amt_krw"] += (amt or 0) * ((o_fx or fx_now) if ledger.endswith("_us") else 1.0)
    return {"trades": list(reversed(ts)), "pnl": round(sum(t["pnl"] for t in ts), 2), "n": len(ts),
            "costs": round(sum(t["cost"] for t in ts), 2),
            "wins": len(wins), "win_rate": round(len(wins) / len(ts) * 100, 1) if ts else None,
            "best": max(ts, key=lambda t: t["pnl_pct"]) if ts else None,       # 화면에 %로 보여 주니 % 기준으로
            "worst": min(ts, key=lambda t: t["pnl_pct"]) if ts else None,
            "by_code": sorted(by_code.values(), key=lambda b: -b["pnl"]),
            "by_day": sorted(by_day.items()), "buys": buys["n"], "buy_amount": buys["amt"],
            "buy_amount_krw": round(buys["amt_krw"]), "pnl_krw": sum(t["pnl_krw"] for t in ts),
            "costs_krw": sum(t["cost_krw"] for t in ts)}
