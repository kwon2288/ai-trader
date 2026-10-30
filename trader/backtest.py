"""백테스트 — 지금의 규칙 신호 + 손절·익절 설정을 과거 일봉으로 돌려 본다 (AI 없음).

가정 (실제와 다른 점)
  - 신호는 그날 종가로 판단하고 **다음 날 시가**에 사고판다 (미래 데이터를 쓰지 않음).
  - 손절·익절은 장중 감시처럼 그날 저가·고가가 선에 닿으면 그 가격(갭이면 시가)에 판다.
  - 실제 봇은 신호 난 종목을 AI가 한 번 더 걸러서 매매 수가 더 적다. 경고 종목·자동 선정 변화는 반영 안 함.
  - 수수료·세금은 설정 비율. 체결은 항상 된다고 본다.
"""
from __future__ import annotations

import json
import math
import time
from datetime import datetime
from pathlib import Path

from . import indicators, signals
from .journal import KST
from .ledger import _ETF, costs_of

WARMUP = 60          # 지표 계산에 필요한 앞부분


def load_bars(broker, code: str, days: int, cache_dir: Path) -> list[dict]:
    """하루 한 번만 받아 data/bars 에 저장해 둔다."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    f = cache_dir / f"{code}.json"
    today = datetime.now(KST).date().isoformat()
    if f.exists():
        try:
            d = json.loads(f.read_text())
            # 오늘 받은 것이고, 그때 요청한 기간이 지금 요청 이상일 때만 재사용
            # (손익 화면이 짧게 받아 둔 지수 데이터를 백테스트가 그대로 쓰던 문제 방지)
            if d.get("date") == today and d.get("asked", 0) >= days:
                return d["bars"][-days:]
        except ValueError:
            pass
    fn = getattr(broker, "history", None)
    bars = fn(code, days) if fn else broker.daily(code, days)
    f.write_text(json.dumps({"date": today, "asked": days, "bars": bars}))
    return bars


def run(cfg, data: dict[str, list[dict]], names: dict[str, str] | None = None, *, stop_pct=None, take_pct=None,
        bench: list[dict] | None = None) -> dict:
    """data: 종목코드 → 일봉(오래된→최신). 반환: 결과 요약·자산 곡선·거래 내역."""
    names = names or {}
    stop = cfg.stop_loss_pct if stop_pct is None else stop_pct
    take = cfg.take_profit_pct if take_pct is None else take_pct
    budget, per, maxpos = float(cfg.total_krw), float(cfg.max_per_position_krw), int(cfg.max_positions)
    costs = costs_of(cfg)
    us = cfg.market == "us"
    fee = (costs["us_fee_pct"] if us else costs["kr_fee_pct"]) / 100

    def tax(code):
        return 0.0 if us or _ETF.search(names.get(code, "")) else costs["kr_sell_tax_pct"] / 100

    by_date = {c: {b["date"]: i for i, b in enumerate(bars)} for c, bars in data.items()}
    dates = sorted({b["date"] for bars in data.values() for b in bars})
    pos: dict[str, dict] = {}          # code → {qty, cost(수수료 포함), avg, day}
    realized, trades, curve = 0.0, [], []
    paid = {"fees": 0.0}
    pending: list[tuple[str, str, float]] = []   # (BUY/SELL, code, strength) → 다음 날 시가에

    def sell(code, px, d, why):
        nonlocal realized
        p = pos.pop(code)
        gross = p["qty"] * px
        cost_out = gross * (fee + tax(code))
        paid["fees"] += cost_out + p["qty"] * p["avg"] * fee
        pnl = gross - cost_out - p["cost"]
        realized += pnl
        trades.append({"code": code, "name": names.get(code, code), "buy_date": p["day"], "sell_date": d,
                       "qty": p["qty"], "buy": round(p["avg"], 4), "sell": round(px, 4), "pnl": round(pnl, 2),
                       "pnl_pct": round(pnl / p["cost"] * 100, 2) if p["cost"] else 0.0, "why": why})

    for di, d in enumerate(dates):
        # 1) 어제 신호를 오늘 시가에 실행 (매도 먼저)
        for side, code, _s in sorted(pending, key=lambda x: (x[0] != "SELL", -x[2])):
            i = by_date[code].get(d)
            if i is None:
                continue
            o = data[code][i]["open"] or data[code][i]["close"]
            if side == "SELL" and code in pos:
                sell(code, o, d, "규칙 매도")
            elif side == "BUY" and code not in pos and len(pos) < maxpos:
                invested = sum(p["cost"] for p in pos.values())
                money = min(per, budget - invested)
                qty = math.floor(money / (o * (1 + fee))) if o else 0
                if qty >= 1:
                    pos[code] = {"qty": qty, "cost": qty * o * (1 + fee), "avg": o, "day": d}
        pending = []
        # 2) 장중 손절·익절 (저가·고가 기준)
        for code in list(pos):
            i = by_date[code].get(d)
            if i is None:
                continue
            b, p = data[code][i], pos[code]
            sp, tp = p["avg"] * (1 + stop / 100), p["avg"] * (1 + take / 100)
            if b["low"] and b["low"] <= sp:
                sell(code, min(b["open"] or sp, sp), d, "손절")
            elif b["high"] and b["high"] >= tp:
                sell(code, max(b["open"] or tp, tp), d, "익절")
        # 3) 오늘 종가로 신호 → 내일 실행
        cands = []
        for code, bars in data.items():
            i = by_date[code].get(d)
            if i is None or i < WARMUP:
                continue
            ind = indicators.compute(bars[max(0, i - 79):i + 1])
            prev = bars[i - 1]["close"] if i else None
            cands.append({"code": code, "held": code in pos, "warnings": [],
                          "affordable": bars[i]["close"] <= per,
                          "today_change_pct": round((bars[i]["close"] / prev - 1) * 100, 2) if prev else None, **ind})
        min_hold = int(getattr(cfg, "min_hold_days", 0) or 0)
        for code, s in signals.evaluate(cands, can_buy=len(pos) < maxpos).items():
            if s["type"].startswith("SELL"):
                held_days = (datetime.strptime(d, "%Y%m%d") - datetime.strptime(pos[code]["day"], "%Y%m%d")).days \
                    if code in pos else 0
                if held_days >= min_hold:
                    pending.append(("SELL", code, s["strength"]))
            else:
                pending.append(("BUY", code, s["strength"]))
        # 4) 자산 (예산 + 실현 + 평가)
        unreal = 0.0
        for code, p in pos.items():
            i = by_date[code].get(d)
            px = data[code][i]["close"] if i is not None else p["avg"]
            unreal += p["qty"] * px * (1 - fee - tax(code)) - p["cost"]
        if di >= WARMUP:
            curve.append({"date": d, "equity": round(budget + realized + unreal, 2)})

    eq = [c["equity"] for c in curve] or [budget]
    peak, mdd = eq[0], 0.0
    for v in eq:
        peak = max(peak, v)
        mdd = min(mdd, (v / peak - 1) * 100)
    wins = [t for t in trades if t["pnl"] > 0]
    by_code: dict[str, dict] = {}
    for t in trades:
        b = by_code.setdefault(t["code"], {"code": t["code"], "name": t["name"], "n": 0, "pnl": 0.0, "wins": 0})
        b["n"] += 1
        b["pnl"] = round(b["pnl"] + t["pnl"], 2)
        b["wins"] += int(t["pnl"] > 0)
    # 데이터 점검: 기간이 짧거나, 하루 ±35% 넘는 움직임(액면분할 미반영·데이터 오류 의심)
    checks = []
    for code, bars in data.items():
        jumps = [b["date"] for a, b in zip(bars, bars[1:]) if a["close"] and abs(b["close"] / a["close"] - 1) > 0.35]
        checks.append({"code": code, "name": names.get(code, code), "bars": len(bars),
                       "first": bars[0]["date"] if bars else "", "jumps": jumps[:3]})
    short = [c for c in checks if c["bars"] < len(dates) * 0.8]
    res = {"start": curve[0]["date"] if curve else None, "end": curve[-1]["date"] if curve else None,
           "days": len(curve), "return_pct": round((eq[-1] / budget - 1) * 100, 2), "pnl": round(eq[-1] - budget, 2),
           "mdd_pct": round(mdd, 2), "trades": list(reversed(trades)), "n": len(trades),
           "win_rate": round(len(wins) / len(trades) * 100, 1) if trades else None,
           "avg_win": round(sum(t["pnl_pct"] for t in wins) / len(wins), 2) if wins else None,
           "avg_loss": round(sum(t["pnl_pct"] for t in trades if t["pnl"] <= 0) / max(1, len(trades) - len(wins)), 2)
           if len(trades) > len(wins) else None,
           "open_positions": len(pos), "curve": curve, "stop": stop, "take": take, "symbols": len(data),
           "fees": round(paid["fees"], 2),
           "gross_return_pct": round((eq[-1] + paid["fees"]) / budget * 100 - 100, 2),
           "fee_pct": round(fee * 100, 3), "by_code": sorted(by_code.values(), key=lambda b: b["pnl"]),
           "checks": checks, "warn_short": [c["name"] for c in short],
           "warn_jumps": [f"{c['name']}({', '.join(c['jumps'])})" for c in checks if c["jumps"]]}
    if bench and curve:
        bmap = {b["date"]: b["close"] for b in bench}
        b0 = next((bmap[c["date"]] for c in curve if c["date"] in bmap), None)
        b1 = next((bmap[c["date"]] for c in reversed(curve) if c["date"] in bmap), None)
        res["bench_pct"] = round((b1 / b0 - 1) * 100, 2) if b0 and b1 else None
        res["bench_curve"] = [{"date": c["date"], "v": round(budget * bmap[c["date"]] / b0, 2)}
                              for c in curve if c["date"] in bmap] if b0 else []
    return res


def save(data_dir: Path, market: str, res: dict, params: dict) -> None:
    d = {"ts": datetime.now(KST).isoformat(timespec="seconds"), "params": params, **res}
    (Path(data_dir) / f"backtest_{market}.json").write_text(json.dumps(d, ensure_ascii=False))


def load(data_dir: Path, market: str) -> dict | None:
    try:
        return json.loads((Path(data_dir) / f"backtest_{market}.json").read_text())
    except (OSError, ValueError):
        return None


def run_job(cfg, broker, days: int, stop_pct: float, take_pct: float, symbols: list[str], bench_code: str,
            progress=None) -> dict:
    """데이터를 받아(캐시) 백테스트하고 결과를 저장."""
    t0 = time.time()
    cache = Path(cfg.data_dir) / "bars" / cfg.market
    data, names = {}, {}
    total = days + WARMUP + 10
    for k, code in enumerate(symbols, 1):
        if progress:
            progress(k, len(symbols), code)
        try:
            bars = load_bars(broker, code, total, cache)
        except Exception:  # noqa: BLE001
            continue
        if len(bars) > WARMUP + 20:
            data[code] = bars
        try:
            names[code] = broker.quote(code).name
        except Exception:  # noqa: BLE001
            names[code] = code
    bench = None
    if bench_code:
        try:
            bench = load_bars(broker, bench_code, total, cache)
        except Exception:  # noqa: BLE001
            bench = None
    res = run(cfg, data, names, stop_pct=stop_pct, take_pct=take_pct, bench=bench)
    res["seconds"] = round(time.time() - t0, 1)
    res["bench_code"] = bench_code
    save(cfg.data_dir, cfg.market, res, {"days": days, "stop": stop_pct, "take": take_pct, "symbols": symbols})
    return res
