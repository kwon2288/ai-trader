"""재현 점검 — 백테스트가 실시간에서도 그대로 나오는가?

그림자 장부(규칙만, 실시간 가상 매매 — shadow.py)가 시작된 날부터 오늘까지를 지금 설정 그대로 백테스트해서 나란히 본다.
둘 다 ‘AI 없이 규칙만’이라, 차이가 나면 AI 가 아니라 백테스트 가정(다음 날 시가 체결·종가 신호·슬리피지 등) 때문이다.
  · 신호 일치율: 양쪽 매수 중 같은 종목을 비슷한 날(백테스트는 신호 다음 날 시가라 −1~+4일 차이 허용) 산 비율
  · 진입 가격 차이: 일치한 매수에서 실시간 가격이 백테스트 시가보다 몇 % 비쌌나(+)/쌌나(−)
  · 수익률 차이: 같은 기간 예산 대비 수익률 (평가 손익 포함)
통과 기준(제안): 그림자 매수 10건 이상에서 일치율 70% 이상 · 수익률 차이 ±3%p 이내.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

from .journal import KST

MIN_TRADES = 10
PASS_MATCH, PASS_GAP = 70.0, 3.0


def _d(s: str) -> date:
    s = s.replace("-", "")[:8]
    return date(int(s[:4]), int(s[4:6]), int(s[6:8]))


def _shadow_buys(j, ledger: str) -> list[dict]:
    from . import shadow
    shadow._ensure(j)
    out = [{"code": r["code"], "name": r["name"], "day": r["opened"], "price": r["buy"]}
           for r in j.db.execute("SELECT code, name, opened, buy FROM shadow_trades WHERE ledger=?", (ledger,))]
    out += [{"code": p["code"], "name": p["name"], "day": p["opened"], "price": p["avg"]}
            for p in shadow.positions(j, ledger).values()]
    return sorted(out, key=lambda x: x["day"])


def match(bt_buys: list[dict], sh_buys: list[dict]) -> dict:
    """같은 종목 · 날짜 차이 −1~+4일(달력)이면 같은 매수로 본다."""
    used = set()
    pairs = []
    for s in sh_buys:
        best = None
        for i, b in enumerate(bt_buys):
            if i in used or b["code"] != s["code"]:
                continue
            gap = (_d(b["day"]) - _d(s["day"])).days
            if -1 <= gap <= 4 and (best is None or abs(gap) < best[1]):
                best = (i, abs(gap))
        if best:
            used.add(best[0])
            b = bt_buys[best[0]]
            pairs.append({"code": s["code"], "name": s["name"], "shadow_day": s["day"], "bt_day": b["day"],
                          "shadow_px": s["price"], "bt_px": b["price"],
                          "px_diff_pct": round((s["price"] / b["price"] - 1) * 100, 2) if b["price"] else None})
    matched_sh = {(p["code"], p["shadow_day"]) for p in pairs}
    only_sh = [s for s in sh_buys if (s["code"], s["day"]) not in matched_sh]
    only_bt = [b for i, b in enumerate(bt_buys) if i not in used]
    union = len(pairs) + len(only_sh) + len(only_bt)
    diffs = [p["px_diff_pct"] for p in pairs if p["px_diff_pct"] is not None]
    return {"pairs": pairs, "only_shadow": only_sh, "only_bt": only_bt,
            "match_rate": round(len(pairs) / union * 100, 1) if union else None,
            "px_diff_avg": round(sum(diffs) / len(diffs), 2) if diffs else None}


def run(cfg, broker, progress=None) -> dict:
    from . import backtest, shadow, strategy
    from .journal import Journal
    from .ledger import costs_of
    shadow_j = Journal(cfg.db_path)
    start = shadow.start_day(shadow_j, cfg.ledger)
    if not start:
        raise RuntimeError("그림자 장부가 아직 없어요 — AI 를 쓰는 정기 실행이 한 번 돌아야 시작돼요")
    sh_buys = _shadow_buys(shadow_j, cfg.ledger)
    codes = list(dict.fromkeys(list(cfg.symbols) + [b["code"] for b in sh_buys]))
    span = (datetime.now(KST).date() - _d(start)).days
    days = max(30, int(span * 5 / 7) + 5)
    sg = strategy.settings(cfg)
    data, names, bench, _core = backtest.prepare(cfg, broker, days + 90, codes, strategy.BENCH[cfg.market][0],
                                                 progress=progress)
    res = backtest.run(cfg, data, names, bench=bench, market_filter=sg["market_filter"], signal_mode=sg["signal"],
                       max_hold=sg["max_hold_days"], guard=strategy.guard_opts(sg), stop_atr=sg["stop_atr"],
                       slip_pct=costs_of(cfg)["slip_pct"], date_from=start.replace("-", ""))
    bt_buys = [{"code": t["code"], "name": t["name"], "day": t["buy_date"], "price": t["buy"]} for t in res["trades"]]
    bt_buys += [{"code": p["code"], "name": p["name"], "day": p["day"], "price": p["avg"]} for p in res["open_list"]]
    m = match(bt_buys, sh_buys)
    sh = shadow.summary(shadow_j, cfg) or {}
    gap = round(sh.get("pct", 0.0) - res["return_pct"], 2)
    n = len(sh_buys)
    if n < MIN_TRADES:
        verdict = f"그림자 매수 {n}건 — {MIN_TRADES}건쯤 쌓여야 판단할 만해요 (지금 숫자는 참고만)"
        ok = None
    else:
        ok = (m["match_rate"] or 0) >= PASS_MATCH and abs(gap) <= PASS_GAP
        verdict = ("✅ 재현돼요 — 백테스트 결과를 어느 정도 믿어도 돼요" if ok else
                   "⚠ 차이가 커요 — 백테스트 수치를 그대로 믿으면 안 돼요 (아래 ‘백테스트에만/실시간에만’ 거래를 보세요)")
    out = {"ts": datetime.now(KST).isoformat(timespec="seconds"), "start": start, "end": res.get("end"),
           "bt_return_pct": res["return_pct"], "bt_n": len(bt_buys), "shadow_pct": sh.get("pct"), "shadow_n": n,
           "gap": gap, "ok": ok, "verdict": verdict, **m,
           "criteria": f"매수 {MIN_TRADES}건 이상 · 일치율 {PASS_MATCH:g}% 이상 · 수익률 차이 ±{PASS_GAP:g}%p 이내"}
    save(cfg.data_dir, cfg.market, out)
    return out


def save(data_dir, market: str, d: dict) -> None:
    (Path(data_dir) / f"replay_{market}.json").write_text(json.dumps(d, ensure_ascii=False))


def load(data_dir, market: str) -> dict | None:
    try:
        return json.loads((Path(data_dir) / f"replay_{market}.json").read_text())
    except (OSError, ValueError):
        return None
