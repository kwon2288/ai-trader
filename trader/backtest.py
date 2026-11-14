"""백테스트 — 지금의 규칙 신호 + 손절·익절 설정을 과거 일봉으로 돌려 본다 (AI 없음).

가정 (실제와 다른 점)
  - 신호는 그날 종가로 판단하고 **다음 날 시가**에 사고판다 (미래 데이터를 쓰지 않음).
  - 손절·익절은 장중 감시처럼 그날 저가·고가가 선에 닿으면 그 가격(갭이면 시가)에 판다.
  - 실제 봇은 신호 난 종목을 AI가 한 번 더 걸러서 매매 수가 더 적다. 경고 종목·자동 선정 변화는 반영 안 함.
  - 수수료·세금은 설정 비율. 체결은 항상 된다고 본다. 슬리피지(slip_pct)를 주면 살 때 그만큼 비싸게, 팔 때 싸게.
  - 지금 종목 목록으로 과거를 돌린다 — 그사이 상장폐지·관리종목이 된 종목은 빠져 있어 결과가 좋게 나오는 경향(생존 편향).

장세 구분 (지수 기준, 그날 종가): 하락 = 200일선 아래 · 상승 = 200일선 위이고 60거래일 수익 +5% 이상 · 횡보 = 그 밖.
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


def _regime_map(bench: list[dict] | None) -> dict[str, bool]:
    """날짜 → 지수 종가가 200일선 이상인지 (그날 종가 기준, 다음 날 매수에 적용)."""
    out = {}
    if not bench:
        return out
    closes = [b["close"] for b in bench]
    run_sum = 0.0
    for i, b in enumerate(bench):
        run_sum += closes[i]
        if i >= 200:
            run_sum -= closes[i - 200]
        if i >= 199:
            out[b["date"]] = closes[i] >= run_sum / 200
    return out


def _regime3(bench: list[dict] | None) -> dict[str, str]:
    """날짜 → 'up' | 'side' | 'down' (위 설명)."""
    out: dict[str, str] = {}
    if not bench:
        return out
    closes = [b["close"] for b in bench]
    run_sum = 0.0
    for i, b in enumerate(bench):
        run_sum += closes[i]
        if i >= 200:
            run_sum -= closes[i - 200]
        if i >= 199:
            ma = run_sum / 200
            if closes[i] < ma:
                out[b["date"]] = "down"
            elif i >= 60 and closes[i - 60] and (closes[i] / closes[i - 60] - 1) * 100 >= 5:
                out[b["date"]] = "up"
            else:
                out[b["date"]] = "side"
    return out


REGIME_KO = {"up": "상승장", "side": "횡보장", "down": "하락장"}


def metrics(curve: list[dict], trades: list[dict], budget: float, bench: list[dict] | None = None) -> dict:
    """위험조정 지표 + 장세별 성과.
    sharpe·sortino: 일간 수익률 기준 연환산(√252, 무위험 수익률 0) · cagr: 연환산 수익률 ·
    profit_factor: 이긴 거래 합 ÷ 진 거래 합 · max_losing_streak: 연속 손실 거래 수 ·
    regimes: {up/side/down: {days, ret, bench, mdd}} — 그 장세에 해당하는 날들만 이어 붙인 수익률·낙폭."""
    out: dict = {}
    eq = [c["equity"] for c in curve]
    base = [budget] + eq if budget else eq             # 첫날 수익률도 넣으려고 시작 예산부터
    rets = [base[i] / base[i - 1] - 1 for i in range(1, len(base)) if base[i - 1]]
    if len(rets) >= 20:
        mu = sum(rets) / len(rets)
        sd = math.sqrt(sum((r - mu) ** 2 for r in rets) / (len(rets) - 1))
        dn = [min(0.0, r) for r in rets]
        dsd = math.sqrt(sum(r * r for r in dn) / len(dn))
        out["sharpe"] = round(mu / sd * math.sqrt(252), 2) if sd else None
        out["sortino"] = round(mu / dsd * math.sqrt(252), 2) if dsd else None
    if eq and budget and len(eq) > 1 and eq[-1] > 0:
        out["cagr"] = round(((eq[-1] / budget) ** (252 / len(eq)) - 1) * 100, 2)
    gain = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    loss = -sum(t["pnl"] for t in trades if t["pnl"] <= 0)
    out["profit_factor"] = round(gain / loss, 2) if loss else (None if not gain else 99.0)
    streak = best = 0
    for t in sorted(trades, key=lambda t: t["sell_date"]):
        streak = streak + 1 if t["pnl"] <= 0 else 0
        best = max(best, streak)
    out["max_losing_streak"] = best
    reg = _regime3(bench)
    if reg and curve:
        bmap = {b["date"]: b["close"] for b in bench}
        acc = {k: {"days": 0, "g": 1.0, "b": 1.0, "peak": 1.0, "mdd": 0.0} for k in REGIME_KO}
        for i in range(1, len(curve)):
            d, prev = curve[i]["date"], curve[i - 1]["date"]
            k = reg.get(d)
            if not k or not curve[i - 1]["equity"]:
                continue
            a = acc[k]
            a["days"] += 1
            a["g"] *= curve[i]["equity"] / curve[i - 1]["equity"]
            if bmap.get(d) and bmap.get(prev):
                a["b"] *= bmap[d] / bmap[prev]
            a["peak"] = max(a["peak"], a["g"])
            a["mdd"] = min(a["mdd"], (a["g"] / a["peak"] - 1) * 100)
        out["regimes"] = {k: {"days": a["days"], "ret": round((a["g"] - 1) * 100, 2),
                              "bench": round((a["b"] - 1) * 100, 2), "mdd": round(a["mdd"], 2)}
                          for k, a in acc.items()}
    return out


def run(cfg, data: dict[str, list[dict]], names: dict[str, str] | None = None, *, stop_pct=None, take_pct=None,
        bench: list[dict] | None = None, min_hold: int | None = None, market_filter: bool = False,
        core_pct: float = 0, core_code: str | None = None, core_bars: list[dict] | None = None,
        ind_cache: dict | None = None, signal_mode: str = "trend", max_hold: int = 0,
        date_from: str | None = None, date_to: str | None = None, guard: dict | None = None,
        stop_atr: float = 0, slip_pct: float = 0) -> dict:
    """data: 종목코드 → 일봉(오래된→최신). 반환: 결과 요약·자산 곡선·거래 내역.
    date_from/date_to(YYYYMMDD): 그 기간에만 사고판다 (지표는 앞 데이터로 계산) — 앞/뒤 기간 검증용.
    stop_atr: 손절을 ‘전날 ATR14 × 배수’로 (살 때 정해 두고 고정, 0 = stop_pct 고정 손절).
    guard 의 reentry_days: 손실로 판 종목은 그 거래일 수만큼 다시 사지 않는다."""
    names = names or {}
    stop = cfg.stop_loss_pct if stop_pct is None else stop_pct
    take = cfg.take_profit_pct if take_pct is None else take_pct
    budget, per, maxpos = float(cfg.total_krw), float(cfg.max_per_position_krw), int(cfg.max_positions)
    core_budget = budget * max(0.0, min(90.0, float(core_pct or 0))) / 100
    sat_budget = budget - core_budget
    regime = _regime_map(bench) if market_filter else {}
    core_map = {b["date"]: b for b in (core_bars or [])} if core_budget else {}
    core = {"qty": 0, "cost": 0.0, "last": 0.0}
    ind_cache = ind_cache if ind_cache is not None else {}
    costs = costs_of(cfg)
    us = cfg.market == "us"
    fee = (costs["us_fee_pct"] if us else costs["kr_fee_pct"]) / 100
    slip = max(0.0, float(slip_pct or 0)) / 100

    def tax(code):
        return 0.0 if us or _ETF.search(names.get(code, "")) else costs["kr_sell_tax_pct"] / 100

    by_date = {c: {b["date"]: i for i, b in enumerate(bars)} for c, bars in data.items()}
    dates = sorted({b["date"] for bars in data.values() for b in bars})
    day_no = {d: i for i, d in enumerate(dates)}
    pos: dict[str, dict] = {}          # code → {qty, cost(수수료 포함), avg, day}
    realized, trades, curve = 0.0, [], []
    paid = {"fees": 0.0}
    pending: list[tuple] = []   # (BUY/SELL, code, strength, 사유, 돌파선, ATR%) → 다음 날 시가에
    from .strategy import protect_floor, stop_pct as _stop_for
    reentry = int((guard or {}).get("reentry_days") or 0)
    last_loss: dict[str, int] = {}     # code → 손실 매도한 날의 순번 (재매수 대기용)
    filter_missing: list[str] = []     # 시장 필터를 켰는데 지수 판단이 없던 날

    def sell(code, px, d, why):
        nonlocal realized
        p = pos.pop(code)
        px = px * (1 - slip)                       # 슬리피지: 팔 때 조금 싸게
        gross = p["qty"] * px
        cost_out = gross * (fee + tax(code))
        paid["fees"] += cost_out + p["qty"] * p["avg"] * fee
        pnl = gross - cost_out - p["cost"]
        realized += pnl
        if pnl < 0:
            last_loss[code] = day_no[d]
        trades.append({"code": code, "name": names.get(code, code), "buy_date": p["day"], "sell_date": d,
                       "qty": p["qty"], "buy": round(p["avg"], 4), "sell": round(px, 4), "pnl": round(pnl, 2),
                       "pnl_pct": round(pnl / p["cost"] * 100, 2) if p["cost"] else 0.0, "why": why})

    for di, d in enumerate(dates):
        if date_to and d > date_to:
            break
        if date_from and d < date_from:
            continue
        # 1) 어제 신호를 오늘 시가에 실행 (매도 먼저)
        for side, code, _s, why, lvl, atr in sorted(pending, key=lambda x: (x[0] != "SELL", -x[2])):
            i = by_date[code].get(d)
            if i is None:
                continue
            o = data[code][i]["open"] or data[code][i]["close"]
            if side == "SELL" and code in pos:
                sell(code, o, d, why)
            elif side == "BUY" and code not in pos and len(pos) < maxpos:
                o = o * (1 + slip)                 # 슬리피지: 살 때 조금 비싸게
                invested = sum(p["cost"] for p in pos.values())
                money = min(per, sat_budget - invested)
                qty = math.floor(money / (o * (1 + fee))) if o else 0
                if qty >= 1:
                    pos[code] = {"qty": qty, "cost": qty * o * (1 + fee), "avg": o, "day": d, "peak": o, "level": lvl,
                                 "stop": _stop_for(stop, stop_atr, atr)}
        pending = []
        # 코어 ETF: 시뮬레이션 첫날 시가에 코어 예산만큼 사서 끝까지 보유
        if core_budget and not core["qty"] and di >= WARMUP and d in core_map:
            o = core_map[d]["open"] or core_map[d]["close"]
            q = math.floor(core_budget / (o * (1 + fee))) if o else 0
            if q >= 1:
                core.update(qty=q, cost=q * o * (1 + fee))
                paid["fees"] += q * o * fee
        if d in core_map:
            core["last"] = core_map[d]["close"]
        # 2) 장중 손절·익절 (저가·고가 기준)
        for code in list(pos):
            i = by_date[code].get(d)
            if i is None:
                continue
            b, p = data[code][i], pos[code]
            sp, tp = p["avg"] * (1 + p["stop"] / 100), p["avg"] * (1 + take / 100)
            fl = protect_floor(p["avg"], p["peak"], guard)      # 어제까지의 고점 기준 (같은 날 고가·저가 순서는 모름)
            if fl and b["low"] and b["low"] <= fl:
                sell(code, min(b["open"] or fl, fl), d, "이익 보호")
            elif b["low"] and b["low"] <= sp:
                sell(code, min(b["open"] or sp, sp), d, "손절")
            elif b["high"] and b["high"] >= tp:
                sell(code, max(b["open"] or tp, tp), d, "익절")
            else:
                p["peak"] = max(p["peak"], b["high"] or b["close"])
        # 3) 오늘 종가로 신호 → 내일 실행
        cands = []
        for code, bars in data.items():
            i = by_date[code].get(d)
            if i is None or i < WARMUP:
                continue
            ind = ind_cache.get((code, i))
            if ind is None:
                ind = ind_cache[(code, i)] = indicators.compute(bars[max(0, i - 79):i + 1])
            prev = bars[i - 1]["close"] if i else None
            cool = (reentry and code not in pos and code in last_loss and di - last_loss[code] < reentry)
            cands.append({"code": code, "held": code in pos, "warnings": [],
                          "cooldown": f"{reentry}거래일" if cool else None,
                          "breakout_level": pos[code].get("level") if code in pos else None,
                          "affordable": bars[i]["close"] <= per,
                          "today_change_pct": round((bars[i]["close"] / prev - 1) * 100, 2) if prev else None, **ind})
        hold_days = int(getattr(cfg, "min_hold_days", 0) or 0) if min_hold is None else int(min_hold)
        if market_filter:                  # 지수 데이터가 없는 날은 사지 않음 (fail-closed) — 그런 날 수를 결과에 경고
            known = regime.get(d)
            if known is None:
                filter_missing.append(d)
            buy_ok = len(pos) < maxpos and known is True
        else:
            buy_ok = len(pos) < maxpos
        def held_for(code):
            return (datetime.strptime(d, "%Y%m%d") - datetime.strptime(pos[code]["day"], "%Y%m%d")).days

        sig = signals.evaluate(cands, can_buy=buy_ok, mode=signal_mode, opts=guard)
        levels = {c["code"]: c.get("high_20d_prev") for c in cands} if signal_mode == "breakout" else {}
        atrs = {c["code"]: c.get("atr14_pct") for c in cands} if stop_atr else {}
        for code, s in sig.items():
            if s["type"].startswith("SELL"):
                if code in pos and (s.get("urgent") or held_for(code) >= hold_days):
                    pending.append(("SELL", code, s["strength"],
                                    "돌파 실패" if s["type"] == "SELL_FAILED_BREAKOUT" else "규칙 매도", None, None))
            else:
                pending.append(("BUY", code, s["strength"], "", levels.get(code), atrs.get(code)))
        if max_hold:                       # 보유기간 만료 → 다음 날 시가에 정리
            for code in pos:
                if held_for(code) >= max_hold and not (code in sig and sig[code]["type"].startswith("SELL")):
                    pending.append(("SELL", code, 1.0, "보유기간 만료", None, None))
        # 4) 자산 (예산 + 실현 + 평가)
        unreal = 0.0
        for code, p in pos.items():
            i = by_date[code].get(d)
            px = data[code][i]["close"] if i is not None else p["avg"]
            unreal += p["qty"] * px * (1 - fee - tax(code)) - p["cost"]
        if core["qty"]:
            unreal += core["qty"] * core["last"] * (1 - fee - (0.0 if us or _ETF.search(names.get(core_code or "", "") or "ETF")
                                                              else costs["kr_sell_tax_pct"] / 100)) - core["cost"]
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
           "open_positions": len(pos), "open_list": [{"code": c, "name": names.get(c, c), "day": p["day"],
                                                      "avg": round(p["avg"], 4)} for c, p in pos.items()],
           "curve": curve, "stop": stop, "take": take, "symbols": len(data),
           "stop_atr": float(stop_atr or 0),
           "min_hold": hold_days, "market_filter": market_filter, "signal": signal_mode, "max_hold": int(max_hold or 0),
           "guard": bool(guard), "by_why": {w: sum(1 for t in trades if t["why"] == w) for w in {t["why"] for t in trades}},
           "avg_trade_pct": round(sum(t["pnl_pct"] for t in trades) / len(trades), 2) if trades else None, "core_pct": round(core_budget / budget * 100) if budget else 0,
           "core_code": core_code if core_budget else None,
           "core_pnl": round(core["qty"] * core["last"] - core["cost"], 2) if core["qty"] else 0,
           "fees": round(paid["fees"], 2),
           "gross_return_pct": round((eq[-1] + paid["fees"]) / budget * 100 - 100, 2),
           "fee_pct": round(fee * 100, 3), "by_code": sorted(by_code.values(), key=lambda b: b["pnl"]),
           "checks": checks, "warn_short": [c["name"] for c in short], "slip_pct": round(slip * 100, 3),
           "filter_missing_days": len(filter_missing),
           "filter_missing_range": [filter_missing[0], filter_missing[-1]] if filter_missing else None,
           "core_target": round(core_budget, 2), "core_invested": round(core["cost"], 2),
           "core_idle": round(core_budget - core["cost"], 2) if core_budget else 0,
           "core_missing": bool(core_budget) and not core["qty"],
           "warn_jumps": [f"{c['name']}({', '.join(c['jumps'])})" for c in checks if c["jumps"]]}
    if bench and curve:
        bmap = {b["date"]: b["close"] for b in bench}
        b0 = next((bmap[c["date"]] for c in curve if c["date"] in bmap), None)
        b1 = next((bmap[c["date"]] for c in reversed(curve) if c["date"] in bmap), None)
        res["bench_pct"] = round((b1 / b0 - 1) * 100, 2) if b0 and b1 else None
        res["bench_curve"] = [{"date": c["date"], "v": round(budget * bmap[c["date"]] / b0, 2)}
                              for c in curve if c["date"] in bmap] if b0 else []
    res.update(metrics(curve, trades, budget, bench))
    return res


def save(data_dir: Path, market: str, res: dict, params: dict) -> None:
    d = {"ts": datetime.now(KST).isoformat(timespec="seconds"), "params": params, **res}
    (Path(data_dir) / f"backtest_{market}.json").write_text(json.dumps(d, ensure_ascii=False))


def load(data_dir: Path, market: str) -> dict | None:
    try:
        return json.loads((Path(data_dir) / f"backtest_{market}.json").read_text())
    except (OSError, ValueError):
        return None


def prepare(cfg, broker, days: int, symbols: list[str], bench_code: str, core_code: str | None = None,
            progress=None) -> tuple:
    """데이터 준비 (하루 캐시). 지수는 200일선 계산용으로 200일 더."""
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
    bench = core_bars = None
    try:
        bench = load_bars(broker, bench_code, total + 200, cache) if bench_code else None
    except Exception:  # noqa: BLE001
        bench = None
    if core_code:
        try:
            core_bars = bench if core_code == bench_code else load_bars(broker, core_code, total, cache)
            names.setdefault(core_code, broker.quote(core_code).name)
        except Exception:  # noqa: BLE001
            core_bars = None
    return data, names, bench, core_bars


def run_job(cfg, broker, days: int, stop_pct: float, take_pct: float, symbols: list[str], bench_code: str,
            progress=None, min_hold: int | None = None, market_filter: bool | None = None,
            core_pct: float | None = None, signal_mode: str | None = None, max_hold: int | None = None,
            guard: bool | None = None, stop_atr: float | None = None) -> dict:
    """데이터를 받아(캐시) 백테스트하고 결과를 저장. 전략 옵션을 안 주면 설정값."""
    from . import strategy
    sg = strategy.settings(cfg)
    market_filter = sg["market_filter"] if market_filter is None else market_filter
    core_pct = sg["core_pct"] if core_pct is None else core_pct
    signal_mode = sg["signal"] if signal_mode is None else signal_mode
    max_hold = sg["max_hold_days"] if max_hold is None else max_hold
    stop_atr = sg["stop_atr"] if stop_atr is None else stop_atr
    t0 = time.time()
    data, names, bench, core_bars = prepare(cfg, broker, days, symbols, bench_code,
                                            sg["core_code"] if core_pct else None, progress)
    res = run(cfg, data, names, stop_pct=stop_pct, take_pct=take_pct, bench=bench, min_hold=min_hold,
              market_filter=market_filter, core_pct=core_pct, core_code=sg["core_code"], core_bars=core_bars,
              signal_mode=signal_mode, max_hold=max_hold, guard=strategy.guard_opts(sg, guard), stop_atr=stop_atr,
              slip_pct=costs_of(cfg)["slip_pct"])
    res["seconds"] = round(time.time() - t0, 1)
    res["bench_code"] = bench_code
    save(cfg.data_dir, cfg.market, res, {"days": days, "stop": stop_pct, "take": take_pct, "symbols": symbols,
                                          "min_hold": res["min_hold"], "market_filter": market_filter,
                                          "core_pct": core_pct, "signal": signal_mode, "max_hold": max_hold,
                                          "guard": res["guard"], "stop_atr": stop_atr})
    return res


# ── 자동 비교 (여러 조건을 한 번에) ──────────────────────────
# full: 코어-위성까지 넓게 / short: 단기 매매 전용 (코어 0%, 전략 4가지 × 짧은 손절·익절 × 최대 보유일)
GRID = {"signal": ["trend"], "stop": [-5, -8, -12, "atr2"], "take": [10, 20, 35], "min_hold": [2, 7],
        "max_hold": [0], "market_filter": [False, True], "core_pct": [0, 70], "guard": [False, True]}
SHORT_GRID = {"signal": ["trend", "dip", "breakout", "volume"], "stop": [-3, -5, -8, "atr2"], "take": [5, 7, 10, 15],
              "min_hold": [1], "max_hold": [0, 5, 10], "market_filter": [False, True], "core_pct": [0],
              "guard": [False, True]}       # guard = 보호 규칙(추격 차단·돌파 실패 정리·이익 보호·재매수 대기) 끔/켬
# stop 의 "atr2" = 손절을 ‘ATR14 × 2’로 (종목마다 평소 흔들림에 맞춘 손절, 2~15% 사이)
GRIDS = {"full": GRID, "short": SHORT_GRID}


def _atr_mult(v) -> float:
    """격자의 손절 값 "atr2" → 2.0 (숫자면 0 = 고정 손절)."""
    if isinstance(v, str) and v.lower().startswith("atr"):
        try:
            return float(v[3:])
        except ValueError:
            return 0.0
    return 0.0


def grid_size(g: dict) -> int:
    n = 1
    for v in g.values():
        n *= len(v)
    return n


def sweep(cfg, broker, days: int, symbols: list[str], bench_code: str, progress=None, grid: dict | None = None,
          kind: str = "full") -> dict:
    """조건 조합을 모두 돌려 결과 표를 만든다 (데이터·지표는 한 번만 계산)."""
    from itertools import product
    from . import strategy
    g = {**GRIDS.get(kind, GRID), **(grid or {})}
    sg = strategy.settings(cfg)
    t0 = time.time()
    need_core = any(g["core_pct"])
    data, names, bench, core_bars = prepare(cfg, broker, days, symbols, bench_code,
                                            sg["core_code"] if need_core else None, progress)
    ind_cache: dict = {}
    keys = ("signal", "stop", "take", "min_hold", "max_hold", "market_filter", "core_pct", "guard")
    combos = list(product(*(g[k] for k in keys)))
    # 앞/뒤 기간 검증: 앞 2/3 로 순위를 매기고(고르기), 뒤 1/3 에서 그대로 통하는지 본다(확인)
    all_dates = sorted({b["date"] for v in data.values() for b in v})
    usable = all_dates[WARMUP:]
    cut = usable[int(len(usable) * 2 / 3)] if len(usable) >= 60 else None
    is_to = usable[int(len(usable) * 2 / 3) - 1] if cut else None
    rows = []
    for k, (sm, st, tk, mh, xh, mf, cp, gd) in enumerate(combos, 1):
        if progress and (k % 4 == 1 or k == len(combos)):
            progress(k, len(combos), f"조합 {k}/{len(combos)}")
        sa = _atr_mult(st)
        if sa:
            st = float(cfg.stop_loss_pct)          # ATR 을 모를 때만 쓰는 예비 손절
        kw = dict(stop_pct=st, take_pct=tk, stop_atr=sa, bench=bench, min_hold=mh, market_filter=mf, core_pct=cp,
                  core_code=sg["core_code"], core_bars=core_bars, ind_cache=ind_cache, signal_mode=sm, max_hold=xh,
                  guard=strategy.guard_opts(sg, gd), slip_pct=costs_of(cfg)["slip_pct"])
        r = run(cfg, data, names, date_to=is_to, **kw)
        row = {"signal": sm, "stop": st, "stop_atr": sa, "take": tk, "min_hold": mh, "max_hold": xh, "market_filter": mf,
               "core_pct": cp, "guard": gd, "return_pct": r["return_pct"], "mdd_pct": r["mdd_pct"], "n": r["n"],
               "win_rate": r["win_rate"], "fees": r["fees"], "bench_pct": r.get("bench_pct"),
               "avg_trade_pct": r["avg_trade_pct"], "gross_return_pct": r["gross_return_pct"],
               "profit_factor": r.get("profit_factor"), "max_losing_streak": r.get("max_losing_streak")}
        parts = [r]
        if cut:
            o = run(cfg, data, names, date_from=cut, **kw)
            row.update(oos_return_pct=o["return_pct"], oos_mdd_pct=o["mdd_pct"], oos_n=o["n"],
                       oos_bench_pct=o.get("bench_pct"), oos_avg_trade_pct=o["avg_trade_pct"])
            parts.append(o)
        dn = [x["regimes"]["down"] for x in parts if x.get("regimes")]
        if dn and sum(x["days"] for x in dn):              # 하락장 성과 (앞·뒤 기간의 하락장 날들을 이어서)
            g = b = 1.0
            for x in dn:
                g *= 1 + x["ret"] / 100
                b *= 1 + x["bench"] / 100
            row.update(down_days=sum(x["days"] for x in dn), down_ret=round((g - 1) * 100, 2),
                       down_bench=round((b - 1) * 100, 2), down_mdd=min(x["mdd"] for x in dn))
        rows.append(row)
    # 추천: 수익률만 보지 않고 낙폭을 함께 본다 (수익률 − 0.5 × |최대 낙폭|), 거래가 너무 적은 조합은 뒤로
    few = 10 if kind == "short" else 5
    for x in rows:
        x["score"] = round(x["return_pct"] - 0.5 * abs(x["mdd_pct"])
                           - (5 if x["n"] < few and not x["core_pct"] else 0), 2)
    rows.sort(key=lambda x: -x["score"])
    # 전략별 최고 (단기 비교에서 어떤 방식이 이 종목들과 맞는지 한눈에)
    best_by = {}
    for x in rows:
        best_by.setdefault(x["signal"], x)
    out = {"ts": datetime.now(KST).isoformat(timespec="seconds"), "days": days, "symbols": symbols, "kind": kind,
           "bench_pct": rows[0]["bench_pct"] if rows else None, "rows": rows, "seconds": round(time.time() - t0, 1),
           "start": None, "end": None, "fee_pct": rows and costs_of(cfg)["us_fee_pct" if cfg.market == "us" else "kr_fee_pct"],
           "best_by_signal": list(best_by.values()),
           "positive": sum(1 for x in rows if x["return_pct"] > 0),
           "split": {"is_end": is_to, "oos_start": cut} if cut else None}
    out["pick"] = pick(rows)
    if cut:
        top = rows[:10]
        out["validation"] = {
            "top": len(top), "oos_positive": sum(1 for x in top if x["oos_return_pct"] > 0),
            "oos_beat_bench": sum(1 for x in top if x.get("oos_bench_pct") is not None
                                  and x["oos_return_pct"] > x["oos_bench_pct"]),
            "oos_bench_pct": top[0].get("oos_bench_pct") if top else None,
            "oos_avg_top": round(sum(x["oos_return_pct"] for x in top) / len(top), 2) if top else None}
    if data:
        dd = sorted({b["date"] for v in data.values() for b in v})
        out["start"], out["end"] = dd[min(WARMUP, len(dd) - 1)], dd[-1]
    (Path(cfg.data_dir) / f"sweep_{cfg.market}.json").write_text(json.dumps(out, ensure_ascii=False))
    return out


def pick(rows: list[dict]) -> int | None:
    """추천할 줄 번호(0부터). 앞/뒤 기간 검증이 있으면: 앞 기간 순위대로 보면서
    ① 뒤 기간도 플러스이고 지수보다 나은 첫 줄 → ② 없으면 뒤 기간 플러스인 첫 줄 → ③ 없으면 추천 없음(None).
    검증이 없으면(예전 결과) 1위."""
    if not rows:
        return None
    if "oos_return_pct" not in rows[0]:
        return 0
    for i, x in enumerate(rows):
        if x["oos_return_pct"] > 0 and x.get("oos_bench_pct") is not None and x["oos_return_pct"] > x["oos_bench_pct"]:
            return i
    for i, x in enumerate(rows):
        if x["oos_return_pct"] > 0:
            return i
    return None


def load_sweep(data_dir: Path, market: str) -> dict | None:
    try:
        sw = json.loads((Path(data_dir) / f"sweep_{market}.json").read_text())
    except (OSError, ValueError):
        return None
    if "pick" not in sw:                       # 예전에 저장한 결과도 새 기준으로
        sw["pick"] = pick(sw.get("rows") or [])
    return sw
