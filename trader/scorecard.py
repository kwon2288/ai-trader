"""AI 성적표 — 규칙이 낸 매수 신호를 AI 가 통과시켰을 때와 막았을 때, 그 뒤 주가가 어떻게 됐는지 비교한다.

하이브리드(규칙 신호 → AI 확인)에서 AI 가 하는 일은 신호를 거르는 것뿐이다. 그래서
  · AI 가 '사자'고 한 신호의 이후 수익률
  · AI 가 막은 신호의 이후 수익률
을 나란히 보면 AI 가 걸러서 이득인지 손해인지 알 수 있다 (막은 쪽이 더 좋았다면 AI 없이 규칙만 쓰는 편이 낫다).

기록: 신호가 난 날의 가격 → 5거래일 뒤 종가까지의 수익률 (수수료 제외, 실제 매매 여부와 무관).
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .journal import KST

AFTER_DAYS = 5          # 신호 뒤 이 거래일 수만큼 지난 종가로 평가
GIVE_UP_DAYS = 45       # 이만큼 지나도 일봉을 못 구하면 포기

_SCHEMA = """CREATE TABLE IF NOT EXISTS signal_log(
  ledger TEXT, code TEXT, day TEXT, ts TEXT, name TEXT, price REAL, ai_buy INTEGER, note TEXT,
  ret REAL, done INTEGER DEFAULT 0, PRIMARY KEY(ledger, code, day))"""


def _ensure(j) -> None:
    j.db.execute(_SCHEMA)


def record(j, ledger: str, market: str, code: str, name: str, price: float, ai_buy: bool, note: str = "") -> None:
    """오늘(그 시장 거래일) 이 종목의 매수 신호와 AI 판단을 남긴다. 같은 날 다시 판단하면 마지막 것으로."""
    from .fills import market_today
    _ensure(j)
    j.db.execute("INSERT OR REPLACE INTO signal_log(ledger,code,day,ts,name,price,ai_buy,note,ret,done) "
                 "VALUES(?,?,?,?,?,?,?,?,NULL,0)",
                 (ledger, code, market_today(market), datetime.now(KST).isoformat(timespec="seconds"), name,
                  float(price), int(bool(ai_buy)), (note or "")[:200]))
    j.db.commit()


def update(j, cfg, broker, say=None) -> int:
    """평가할 때가 된 신호의 이후 수익률을 채운다 (종목당 일봉 조회 1번). 반환: 채운 건수"""
    _ensure(j)
    today = datetime.now(KST).date()
    cut = (today - timedelta(days=AFTER_DAYS + 2)).isoformat()         # 5거래일 = 달력으로 최소 7일
    rows = [dict(r) for r in j.db.execute(
        "SELECT rowid, code, day, price FROM signal_log WHERE ledger=? AND done=0 AND day <= ?", (cfg.ledger, cut))]
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
        if len(after) >= AFTER_DAYS and r["price"]:
            ret = (float(after[AFTER_DAYS - 1]["close"]) / float(r["price"]) - 1) * 100
            j.db.execute("UPDATE signal_log SET ret=?, done=1 WHERE rowid=?", (round(ret, 2), r["rowid"]))
            n += 1
        elif (today - datetime.fromisoformat(r["day"]).date()).days > GIVE_UP_DAYS:
            j.db.execute("UPDATE signal_log SET done=1 WHERE rowid=?", (r["rowid"],))
    if rows:
        j.db.commit()
    if n and say:
        say(f"📝 AI 성적표: 지난 신호 {n}건의 {AFTER_DAYS}거래일 뒤 수익률을 기록했어요")
    return n


def summary(j, ledger: str) -> dict:
    """{taken: {n, avg, wins}, blocked: {...}, pending, rows(최근 20), verdict}"""
    _ensure(j)
    rows = [dict(r) for r in j.db.execute(
        "SELECT * FROM signal_log WHERE ledger=? ORDER BY day DESC, ts DESC", (ledger,))]

    def grp(flag):
        v = [r["ret"] for r in rows if r["ai_buy"] == flag and r["ret"] is not None]
        return {"n": len(v), "avg": round(sum(v) / len(v), 2) if v else None,
                "wins": sum(1 for x in v if x > 0)}
    t, b = grp(1), grp(0)
    verdict = None
    if t["n"] + b["n"] < 20 or not t["n"] or not b["n"]:
        verdict = f"아직 표본이 적어요 (평가 끝난 신호 {t['n'] + b['n']}건) — 20건쯤 쌓이면 판단할 만해요"
    elif t["avg"] > b["avg"]:
        verdict = f"AI 가 고른 신호가 평균 {t['avg'] - b['avg']:+.2f}%p 더 좋았어요 — AI 확인이 도움이 되고 있어요"
    else:
        verdict = (f"AI 가 막은 신호가 평균 {b['avg'] - t['avg']:+.2f}%p 더 좋았어요 — "
                   f"이대로면 AI 없이 규칙만 쓰는 편이 나아요")
    return {"taken": t, "blocked": b, "pending": sum(1 for r in rows if not r["done"]),
            "rows": rows[:20], "verdict": verdict, "after_days": AFTER_DAYS}
