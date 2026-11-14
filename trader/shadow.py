"""규칙만 그림자 장부 — ‘AI 없이 규칙 신호만 따랐다면?’을 실제 운영과 같은 날·같은 가격·같은 규칙으로 가상 매매한다.

AI 를 쓰는 장부에서만 돈다(규칙만 쓰면 비교할 게 없음). 주문은 절대 내지 않는다.
  · 매수: 규칙 매수 신호(보호 규칙의 추격 차단·재매수 대기 포함)가 나면 그 실행의 가격으로 산다.
          종목당 금액·최대 종목 수·예산은 실제 설정과 같고, 시장 필터·돌파 매수 시점 제한도 같다.
          (봇의 ‘하루 손실 한도’는 봇 자신의 손실 기준이라 그림자에는 적용하지 않는다.)
  · 매도: 손절(고정 % 또는 살 때 정한 ATR 손절)·익절·이익 보호·최대 보유일·규칙 매도 신호 — 실제 봇과 같은 순서.
          장중 감시(15분) 때도 손절·익절·이익 보호를 확인한다.
  · 수수료·세금: 설정의 비율.
비교: 같은 기간 실제 봇(AI) 자산 변화 vs 그림자 자산 변화 (둘 다 예산 대비 %, 평가 손익 포함).
"""
from __future__ import annotations

import logging
import math

from .journal import now

log = logging.getLogger("shadow")

SCHEMA = """
CREATE TABLE IF NOT EXISTS shadow_pos(
  ledger TEXT, code TEXT, name TEXT, qty REAL, avg REAL, cost REAL, opened TEXT, peak REAL, stop REAL, level REAL,
  last REAL, PRIMARY KEY(ledger, code));
CREATE TABLE IF NOT EXISTS shadow_trades(
  id INTEGER PRIMARY KEY, ledger TEXT, code TEXT, name TEXT, opened TEXT, ts TEXT, qty REAL, buy REAL, sell REAL,
  pnl REAL, pnl_pct REAL, reason TEXT);
"""


def _ensure(j) -> None:
    j.db.executescript(SCHEMA)


def positions(j, ledger: str) -> dict[str, dict]:
    _ensure(j)
    return {r["code"]: dict(r) for r in j.db.execute("SELECT * FROM shadow_pos WHERE ledger=?", (ledger,))}


def start_day(j, ledger: str) -> str | None:
    return j.get(f"shadow_start_{ledger}")


def _fee(cfg, side: str, amt: float, name: str) -> float:
    from .ledger import fee_tax, costs_of
    return fee_tax({"side": side, "name": name}, 1, amt, costs_of(cfg), cfg.ledger)


def _sell(j, cfg, p: dict, price: float, reason: str, say) -> None:
    gross = p["qty"] * price
    pnl = gross - _fee(cfg, "SELL", gross, p["name"]) - p["cost"]
    j.db.execute("INSERT INTO shadow_trades(ledger,code,name,opened,ts,qty,buy,sell,pnl,pnl_pct,reason) "
                 "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                 (cfg.ledger, p["code"], p["name"], p["opened"], now().isoformat(timespec="seconds"), p["qty"],
                  p["avg"], price, round(pnl, 2), round((pnl / p["cost"]) * 100, 2) if p["cost"] else 0.0, reason))
    j.db.execute("DELETE FROM shadow_pos WHERE ledger=? AND code=?", (cfg.ledger, p["code"]))
    say(f"👥 그림자(규칙만) 매도 {p['name']} {reason} {(price / p['avg'] - 1) * 100:+.1f}%")


def _exit_reason(cfg, p: dict, price: float, gopts, take: float, max_hold: int) -> str | None:
    from . import strategy
    pnl = (price / p["avg"] - 1) * 100
    if pnl <= p["stop"]:
        return "손절"
    if pnl >= take:
        return "익절"
    floor = strategy.protect_floor(p["avg"], p["peak"], gopts)
    if floor and price <= floor:
        return "이익 보호"
    if max_hold:
        from datetime import date
        if (now().date() - date.fromisoformat(p["opened"])).days >= max_hold:
            return "보유기간 만료"
    return None


def _cash(j, cfg) -> float:
    """그림자 현금 = 예산 + 실현 손익 − 보유 원가 (그림자 시작 이후)."""
    budget = float(cfg.total_krw)
    real = j.db.execute("SELECT COALESCE(SUM(pnl),0) FROM shadow_trades WHERE ledger=?", (cfg.ledger,)).fetchone()[0]
    held = j.db.execute("SELECT COALESCE(SUM(cost),0) FROM shadow_pos WHERE ledger=?", (cfg.ledger,)).fetchone()[0]
    return budget + float(real) - float(held)


def _mark(j, cfg, code: str, price: float) -> None:
    j.db.execute("UPDATE shadow_pos SET last=?, peak=MAX(COALESCE(peak,0), ?) WHERE ledger=? AND code=?",
                 (price, price, cfg.ledger, code))


def watch(j, cfg, prices: dict[str, float], say=print) -> int:
    """장중 감시: 그림자 보유 종목의 손절·익절·이익 보호만 (실제 봇의 장중 감시와 같은 일)."""
    from . import strategy
    _ensure(j)
    sg = strategy.settings(cfg)
    gopts = strategy.guard_opts(sg)
    n = 0
    for code, p in positions(j, cfg.ledger).items():
        px = prices.get(code)
        if not px:
            continue
        _mark(j, cfg, code, px)
        p["peak"] = max(p["peak"] or 0, px)
        why = _exit_reason(cfg, p, px, gopts, cfg.take_profit_pct, 0)
        if why:
            _sell(j, cfg, p, px, why + "(장중)", say)
            n += 1
    j.db.commit()
    return n


def step(j, cfg, candidates: list[dict], prices: dict[str, float], no_buy: str | None, say=print) -> dict:
    """정기 실행 한 번: 청산 → 규칙 매도 신호 → 규칙 매수 신호. candidates 는 실제 봇이 본 후보(지표 포함)."""
    from . import signals, strategy
    _ensure(j)
    if not start_day(j, cfg.ledger):
        j.set(f"shadow_start_{cfg.ledger}", now().date().isoformat())
    sg = strategy.settings(cfg)
    gopts = strategy.guard_opts(sg)
    pos = positions(j, cfg.ledger)
    by_code = {c["code"]: c for c in candidates}
    sold = bought = 0

    # 1) 가격 규칙 청산 (손절·익절·이익 보호·최대 보유일)
    for code, p in list(pos.items()):
        px = prices.get(code)
        if not px:
            continue
        _mark(j, cfg, code, px)
        p["peak"] = max(p["peak"] or 0, px)
        why = _exit_reason(cfg, p, px, gopts, cfg.take_profit_pct, sg["max_hold_days"])
        if why:
            _sell(j, cfg, p, px, why, say)
            pos.pop(code)
            sold += 1

    # 2) 규칙 신호 (그림자 기준 보유 여부로 다시 판단)
    cooled = _cooldown(j, cfg, (gopts or {}).get("reentry_days") or 0)
    view = []
    for c in candidates:
        x = dict(c)
        x["held"] = c["code"] in pos
        x.pop("cooldown", None)
        if not x["held"] and c["code"] in cooled:
            x["cooldown"] = "그림자 재매수 대기"
        if x["held"] and pos[c["code"]].get("level"):
            x["breakout_level"] = pos[c["code"]]["level"]
        view.append(x)
    cash = _cash(j, cfg)
    can_buy = not no_buy and len(pos) < cfg.max_positions
    sig = signals.evaluate(view, can_buy=can_buy, mode=sg["signal"], opts=gopts)
    for code, s in sig.items():
        if s["type"].startswith("SELL") and code in pos and prices.get(code):
            p = pos[code]
            from datetime import date
            held_days = (now().date() - date.fromisoformat(p["opened"])).days
            if held_days < cfg.min_hold_days and not s.get("urgent"):
                continue
            _sell(j, cfg, p, prices[code], "돌파 실패" if s["type"] == "SELL_FAILED_BREAKOUT" else "규칙 매도 신호", say)
            pos.pop(code)
            cash = _cash(j, cfg)
            sold += 1
    if can_buy:
        buys = sorted([(c, s) for c, s in sig.items() if s["type"].startswith("BUY") and c not in pos],
                      key=lambda kv: -kv[1]["strength"])
        for code, s in buys:
            if len(pos) >= cfg.max_positions:
                break
            px = prices.get(code)
            c = by_code.get(code) or {}
            if not px or not c.get("affordable", True):
                continue
            amt = min(float(cfg.max_per_position_krw), cash)
            qty = math.floor(amt / px)
            if qty < 1:
                continue
            gross = qty * px
            cost = gross + _fee(cfg, "BUY", gross, c.get("name", ""))
            if cost > cash:
                continue
            stop = strategy.stop_pct(cfg.stop_loss_pct, sg["stop_atr"], c.get("atr14_pct"))
            level = c.get("high_20d_prev") if sg["signal"] == "breakout" else None
            j.db.execute("INSERT OR REPLACE INTO shadow_pos(ledger,code,name,qty,avg,cost,opened,peak,stop,level,last) "
                         "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                         (cfg.ledger, code, c.get("name", code), qty, px, cost, now().date().isoformat(), px, stop,
                          level, px))
            pos[code] = {"code": code}
            cash -= cost
            bought += 1
            say(f"👥 그림자(규칙만) 매수 {c.get('name', code)} {qty}주 @{cfg.money(px)} — {s['reason']}")
    j.db.commit()
    return {"sold": sold, "bought": bought}


def _cooldown(j, cfg, days: int) -> set[str]:
    if not days:
        return set()
    from . import strategy
    today = now().date().isoformat()
    last: dict[str, tuple] = {}
    for r in j.db.execute("SELECT code, ts, pnl FROM shadow_trades WHERE ledger=? ORDER BY id", (cfg.ledger,)):
        last[r["code"]] = (r["ts"][:10], r["pnl"])
    return {c for c, (d, pnl) in last.items() if pnl < 0 and strategy.busdays_between(d, today) < days}


def summary(j, cfg, start: str | None = None) -> dict | None:
    """그림자 성적 (시작일 또는 start 이후): 실현·평가·합계, 예산 대비 %, 거래 수·승률."""
    _ensure(j)
    s0 = start_day(j, cfg.ledger)
    if not s0:
        return None
    since = max(s0, start or s0)
    trades = [dict(r) for r in j.db.execute(
        "SELECT * FROM shadow_trades WHERE ledger=? AND ts >= ? ORDER BY id", (cfg.ledger, since))]
    realized = sum(t["pnl"] for t in trades)
    pos = positions(j, cfg.ledger)
    unreal = sum((p["last"] or p["avg"]) * p["qty"] - p["cost"] for p in pos.values())
    budget = float(cfg.total_krw) or 1.0
    wins = sum(1 for t in trades if t["pnl"] > 0)
    return {"start": s0, "since": since, "realized": round(realized, 2), "unrealized": round(unreal, 2),
            "total": round(realized + unreal, 2), "pct": round((realized + unreal) / budget * 100, 2),
            "n": len(trades), "wins": wins, "win_rate": round(wins / len(trades) * 100) if trades else None,
            "open": len(pos), "trades": list(reversed(trades))[:10],
            "positions": sorted(pos.values(), key=lambda p: p["opened"])}


def compare(j, cfg, start: str | None = None) -> dict | None:
    """같은 기간 실제 봇(AI) vs 그림자(규칙만). 봇은 정기 실행 자산 기록 기준, AI 비용 뺀 값도."""
    sh = summary(j, cfg, start)
    if not sh:
        return None
    from . import bench, pricing
    from datetime import date
    budget = float(cfg.total_krw) or 1.0
    bot = bench.bot_change(j, cfg.ledger, budget, date.fromisoformat(sh["since"]))
    ai = pricing.ai_cost(j, cfg.data_dir, cfg.ledger, date.fromisoformat(sh["since"]))
    try:
        fx = float(j.get("fx_usdkrw") or 0) or 1400.0
    except ValueError:
        fx = 1400.0
    ai_local = ai["usd"] * (1 if cfg.market == "us" else fx)
    bot_net = None if bot is None else round(bot - ai_local / budget * 100, 2)
    gap = None if bot_net is None else round(bot_net - sh["pct"], 2)
    if gap is None:
        verdict = "아직 비교할 실제 운영 기록이 없어요"
    elif sh["n"] < 10:
        verdict = f"그림자 매도 {sh['n']}건 — 10건쯤 쌓이면 판단할 만해요"
    elif gap > 0:
        verdict = f"AI 가 규칙만보다 {gap:.2f}%p 앞서요 (AI 비용 뺀 뒤)"
    else:
        verdict = f"규칙만이 AI 보다 {-gap:.2f}%p 앞서요 (AI 비용 뺀 뒤) — 이대로면 AI 없이도 충분해요"
    return {**sh, "bot_pct": bot, "bot_net_pct": bot_net, "ai_usd": ai["usd"], "gap": gap, "verdict": verdict}


def line(j, cfg) -> str | None:
    """주간 리포트용 한 줄."""
    try:
        c = compare(j, cfg)
    except Exception as e:  # noqa: BLE001
        log.warning("그림자 비교 실패: %s", e)
        return None
    if not c or c["bot_net_pct"] is None:
        return None
    return (f"AI vs 규칙만({c['start'][5:]}부터): AI {c['bot_net_pct']:+.2f}% (AI 비용 뺀 뒤) · "
            f"규칙만 {c['pct']:+.2f}% → {c['verdict']}")

