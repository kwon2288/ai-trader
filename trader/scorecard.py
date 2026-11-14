"""AI 성적표 — 규칙이 낸 매수 신호를 AI 가 통과시켰을 때와 막았을 때, 그 뒤 주가가 어떻게 됐는지 비교한다.

하이브리드(규칙 신호 → AI 확인)에서 AI 가 하는 일은 신호를 거르는 것뿐이다. 그래서
  · AI 가 '사자'고 한 신호의 이후 수익률
  · AI 가 막은 신호의 이후 수익률
을 나란히 보면 AI 가 걸러서 이득인지 손해인지 알 수 있다 (막은 쪽이 더 좋았다면 AI 없이 규칙만 쓰는 편이 낫다).

기록: 신호가 난 날의 가격 → 1·5·20거래일 뒤 종가까지의 수익률 (수수료 제외, 실제 매매 여부와 무관).
다수결이면 AI 마다 낸 판단(votes)도 남겨 AI 별 적중률을 본다:
  · 사자고 한 신호가 5거래일 뒤 올랐으면 적중, 사지 말자고 한 신호가 오르지 않았으면 적중.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from .journal import KST

AFTER_DAYS = 5          # 신호 뒤 이 거래일 수만큼 지난 종가로 평가 (기본 판정)
HORIZONS = (1, 5, 20)   # 함께 기록하는 기간 (거래일)
GIVE_UP_DAYS = 60       # 이만큼 지나도 일봉을 못 구하면 포기

_SCHEMA = """CREATE TABLE IF NOT EXISTS signal_log(
  ledger TEXT, code TEXT, day TEXT, ts TEXT, name TEXT, price REAL, ai_buy INTEGER, note TEXT,
  ret REAL, done INTEGER DEFAULT 0, PRIMARY KEY(ledger, code, day))"""


def _ensure(j) -> None:
    j.db.execute(_SCHEMA)
    for col in ("ret1 REAL", "ret20 REAL", "votes TEXT"):       # v0.31: 1·20거래일 · AI 별 판단
        try:
            j.db.execute(f"ALTER TABLE signal_log ADD COLUMN {col}")
        except Exception:  # noqa: BLE001
            pass


def record(j, ledger: str, market: str, code: str, name: str, price: float, ai_buy: bool, note: str = "",
           votes: dict | None = None) -> None:
    """오늘(그 시장 거래일) 이 종목의 매수 신호와 AI 판단을 남긴다. 같은 날 다시 판단하면 마지막 것으로.
    votes: {AI 이름: BUY/HOLD/SELL} — 다수결이면 AI 마다, 단일 AI 면 그 하나."""
    from .fills import market_today
    _ensure(j)
    j.db.execute("INSERT OR REPLACE INTO signal_log(ledger,code,day,ts,name,price,ai_buy,note,ret,done,ret1,ret20,votes) "
                 "VALUES(?,?,?,?,?,?,?,?,NULL,0,NULL,NULL,?)",
                 (ledger, code, market_today(market), datetime.now(KST).isoformat(timespec="seconds"), name,
                  float(price), int(bool(ai_buy)), (note or "")[:200],
                  json.dumps(votes, ensure_ascii=False) if votes else None))
    j.db.commit()


def update(j, cfg, broker, say=None) -> int:
    """평가할 때가 된 신호의 이후 수익률을 채운다 (종목당 일봉 조회 1번). 반환: 채운 건수"""
    _ensure(j)
    today = datetime.now(KST).date()
    cut = (today - timedelta(days=2)).isoformat()                       # 1거래일 = 달력으로 최소 1~3일
    rows = [dict(r) for r in j.db.execute(
        "SELECT rowid, code, day, price, ret, ret1, ret20 FROM signal_log WHERE ledger=? AND done=0 AND day <= ?",
        (cfg.ledger, cut))]
    n, bars_of = 0, {}
    for r in rows:
        code = r["code"]
        if code not in bars_of:
            try:
                bars_of[code] = broker.daily(code, 60)
            except Exception:  # noqa: BLE001
                bars_of[code] = None
        bars = bars_of[code] or []
        key = r["day"].replace("-", "")
        after = [b for b in bars if str(b.get("date", "")).replace("-", "") > key and b.get("close")]
        if not r["price"]:
            continue
        got = {}
        for h, col in ((1, "ret1"), (AFTER_DAYS, "ret"), (20, "ret20")):
            if r[col] is None and len(after) >= h:
                got[col] = round((float(after[h - 1]["close"]) / float(r["price"]) - 1) * 100, 2)
        if got:
            done = int(len(after) >= 20)
            j.db.execute(f"UPDATE signal_log SET {', '.join(k + '=?' for k in got)}, done=? WHERE rowid=?",
                         (*got.values(), done, r["rowid"]))
            n += int("ret" in got)
        if (today - datetime.fromisoformat(r["day"]).date()).days > GIVE_UP_DAYS:
            j.db.execute("UPDATE signal_log SET done=1 WHERE rowid=?", (r["rowid"],))
    if rows:
        j.db.commit()
    if n and say:
        say(f"📝 AI 성적표: 지난 신호 {n}건의 {AFTER_DAYS}거래일 뒤 수익률을 기록했어요")
    return n


def _avg(v):
    return round(sum(v) / len(v), 2) if v else None


def per_ai(rows: list[dict]) -> list[dict]:
    """AI 별: 사자고 한 신호의 5거래일 뒤 평균 · 사지 말자고 한 신호의 평균 · 적중률."""
    out: dict[str, dict] = {}
    for r in rows:
        if r.get("ret") is None or not r.get("votes"):
            continue
        try:
            votes = json.loads(r["votes"])
        except ValueError:
            continue
        for who, act in votes.items():
            x = out.setdefault(who, {"ai": who, "buy": [], "pass": [], "hit": 0, "n": 0})
            buy = act == "BUY"
            (x["buy"] if buy else x["pass"]).append(r["ret"])
            x["n"] += 1
            x["hit"] += int((r["ret"] > 0) == buy)
    res = []
    for x in out.values():
        res.append({"ai": x["ai"], "n": x["n"], "hit_rate": round(x["hit"] / x["n"] * 100) if x["n"] else None,
                    "buy_n": len(x["buy"]), "buy_avg": _avg(x["buy"]),
                    "pass_n": len(x["pass"]), "pass_avg": _avg(x["pass"]),
                    "edge": (_avg(x["buy"]) - _avg(x["pass"])) if x["buy"] and x["pass"] else None})
    return sorted(res, key=lambda x: -(x["hit_rate"] or 0))


def summary(j, ledger: str) -> dict:
    """{taken: {n, avg, wins, avg1, avg20}, blocked: {...}, pending, rows(최근 20), verdict, per_ai}"""
    _ensure(j)
    rows = [dict(r) for r in j.db.execute(
        "SELECT * FROM signal_log WHERE ledger=? ORDER BY day DESC, ts DESC", (ledger,))]

    def grp(flag):
        mine = [r for r in rows if r["ai_buy"] == flag]
        v = [r["ret"] for r in mine if r["ret"] is not None]
        return {"n": len(v), "avg": _avg(v), "wins": sum(1 for x in v if x > 0),
                "avg1": _avg([r["ret1"] for r in mine if r.get("ret1") is not None]),
                "n1": sum(1 for r in mine if r.get("ret1") is not None),
                "avg20": _avg([r["ret20"] for r in mine if r.get("ret20") is not None]),
                "n20": sum(1 for r in mine if r.get("ret20") is not None)}
    t, b = grp(1), grp(0)
    verdict = None
    if t["n"] + b["n"] < 20 or not t["n"] or not b["n"]:
        verdict = f"아직 표본이 적어요 (평가 끝난 신호 {t['n'] + b['n']}건) — 20건쯤 쌓이면 판단할 만해요"
    elif t["avg"] > b["avg"]:
        verdict = f"AI 가 고른 신호가 평균 {t['avg'] - b['avg']:+.2f}%p 더 좋았어요 — AI 확인이 도움이 되고 있어요"
    else:
        verdict = (f"AI 가 막은 신호가 평균 {b['avg'] - t['avg']:+.2f}%p 더 좋았어요 — "
                   f"이대로면 AI 없이 규칙만 쓰는 편이 나아요")
    return {"taken": t, "blocked": b, "pending": sum(1 for r in rows if r["ret"] is None),
            "rows": rows[:20], "verdict": verdict, "after_days": AFTER_DAYS, "per_ai": per_ai(rows)}
