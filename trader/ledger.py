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


def trades(j, ledger: str, costs: dict | None = None) -> list[dict]:
    """봇의 매도 체결마다 실현 손익 (주문 기록을 처음부터 평균단가로 계산, 수수료·세금 차감)."""
    costs = costs or DEFAULT_COSTS
    pos: dict[str, list] = {}           # code → [qty, cost(수수료 포함)]
    out = []
    for o in j.db.execute(
            "SELECT * FROM orders WHERE ok=1 AND mode=? AND COALESCE(order_no,'') != 'DRY' "
            "AND COALESCE(status,'') != 'failed' ORDER BY ts, rowid", (ledger,)):
        o = dict(o)
        qty, px = _eff(o)
        if qty <= 0:
            continue
        ft = fee_tax(o, qty, px, costs, ledger)
        q, c = pos.get(o["code"], [0, 0.0])
        if o["side"] == "BUY":
            pos[o["code"]] = [q + qty, c + qty * px + ft]
            continue
        avg = c / q if q else px                       # 기록 없는 매도(예전 데이터)는 손익 0 으로
        sold = min(qty, q) if q else qty
        pnl = (px - avg) * sold - ft
        out.append({"ts": o["ts"], "date": o["ts"][:10], "code": o["code"], "name": o["name"], "qty": sold,
                    "price": px, "avg": round(avg, 4), "pnl": round(pnl, 2), "cost": round(ft, 2),
                    "pnl_pct": round(((px * sold - ft) / (avg * sold) - 1) * 100, 2) if avg else 0.0})
        nq = max(0, q - sold)
        pos[o["code"]] = [nq, avg * nq]
    return out


def realized_total(j, ledger: str, costs: dict | None = None) -> float:
    return round(sum(t["pnl"] for t in trades(j, ledger, costs)), 2)


def status(j, cfg, held: dict, account_cash: float | None) -> dict:
    """held: 봇 몫 보유 (수량·봇 평단·현재가)."""
    budget = float(cfg.total_krw)
    invested = sum(h.qty * h.avg_price for h in held.values())
    unrealized = sum(h.qty * (h.now_price - h.avg_price) for h in held.values())
    costs = costs_of(cfg)
    realized = realized_total(j, cfg.ledger, costs)
    pnl = realized + unrealized
    room = max(0.0, budget - invested)
    acash = None if account_cash is None or account_cash == float("inf") else float(account_cash)
    avail = room if (cfg.mode == "dry" or acash is None) else min(room, acash)
    r = 2 if cfg.currency == "USD" else 0

    def rd(x):
        return round(x, r) if r else int(round(x))
    return {"budget": rd(budget), "invested": rd(invested), "value": rd(invested + unrealized),
            "unrealized": rd(unrealized), "realized": rd(realized), "pnl": rd(pnl),
            "pnl_pct": round(pnl / budget * 100, 2) if budget else 0.0, "equity": rd(budget + pnl),
            "room": rd(room), "avail": rd(avail), "account_cash": None if acash is None else rd(acash),
            "positions": len(held)}


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
    buys = j.db.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(COALESCE(filled_qty, qty)*COALESCE(fill_price, est_price)),0) amt "
        "FROM orders WHERE ok=1 AND mode=? AND side='BUY' AND COALESCE(status,'') NOT IN ('failed') "
        "AND COALESCE(order_no,'') != 'DRY' AND (? IS NULL OR substr(ts,1,10) >= ?) AND (? IS NULL OR substr(ts,1,10) <= ?)",
        (ledger, start and start.isoformat(), start and start.isoformat(),
         end and end.isoformat(), end and end.isoformat())).fetchone()
    return {"trades": list(reversed(ts)), "pnl": round(sum(t["pnl"] for t in ts), 2), "n": len(ts),
            "costs": round(sum(t["cost"] for t in ts), 2),
            "wins": len(wins), "win_rate": round(len(wins) / len(ts) * 100, 1) if ts else None,
            "best": max(ts, key=lambda t: t["pnl"]) if ts else None,
            "worst": min(ts, key=lambda t: t["pnl"]) if ts else None,
            "by_code": sorted(by_code.values(), key=lambda b: -b["pnl"]),
            "by_day": sorted(by_day.items()), "buys": buys["n"], "buy_amount": buys["amt"]}
