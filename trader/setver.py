"""설정 버전 — 매매에 영향을 주는 설정이 바뀔 때마다 버전을 하나 남기고, 주문에 그 번호를 붙인다.
손익 화면에서 ‘어떤 설정으로 산 거래가 얼마 벌었나’를 버전별로 볼 수 있게.

버전에 들어가는 것: 매매 신호 · 손절(고정 %/ATR) · 익절 · 최소/최대 보유일 · 시장 필터 · 코어 비중 ·
보호 규칙(켬/끔과 값) · 돌파 매수 시점 · 규칙 매도 방식 · AI(종류·모델·규칙 필터·생각 수준) · 최소 확신도.
예산·종목당 금액·종목 목록처럼 ‘얼마나·무엇을’은 넣지 않는다 (전략이 아니라 규모).
"""
from __future__ import annotations

import json

SCHEMA = """CREATE TABLE IF NOT EXISTS setting_versions(
  id INTEGER PRIMARY KEY, ledger TEXT, ts TEXT, fp TEXT, label TEXT, entry TEXT);"""

ENTRY_LABEL = {"rules": "규칙만", "hybrid": "규칙 신호 + AI 확인", "ai": "AI 단독 판단"}


def _ensure(j) -> None:
    j.db.executescript(SCHEMA)


def fingerprint(cfg) -> dict:
    from . import strategy
    sg = strategy.settings(cfg)
    prov = cfg.ai_provider
    used = (["claude", "gemini", "openai"] if prov == "consensus" else [prov]) if prov != "rules" else []
    fp = {"signal": sg["signal"], "stop": float(cfg.stop_loss_pct), "stop_atr": sg["stop_atr"],
          "take": float(cfg.take_profit_pct), "min_hold": int(cfg.min_hold_days), "max_hold": sg["max_hold_days"],
          "market_filter": sg["market_filter"], "core_pct": sg["core_pct"], "rule_sell": sg["rule_sell"],
          "guard": sg["guard"], "ai": prov, "hybrid": bool(cfg.ai_hybrid) if prov != "rules" else False,
          "models": {p: cfg.ai_models.get(p) for p in used}, "min_conf": float(cfg.min_confidence)}
    if sg["guard"]:
        fp["guard_vals"] = {k: sg[k] for k in ("chase_max_ret5", "protect_at_pct", "trail_pct", "reentry_days")}
    if sg["signal"] == "breakout":
        fp["late_buy"] = sg["late_buy"]
    if "gemini" in used and getattr(cfg, "gemini_thinking", ""):
        fp["gemini_thinking"] = cfg.gemini_thinking
    return fp


def entry_kind(fp: dict) -> str:
    if fp.get("ai") == "rules":
        return "rules"
    return "hybrid" if fp.get("hybrid") else "ai"


def label(fp: dict) -> str:
    """사람이 읽는 한 줄: ‘눌림목 반등 · 손절 -5% · 익절 +10% · 최대 10일 · 보호 · 다수결 + 규칙 필터’."""
    from .signals import label as sig_label
    parts = [sig_label(fp["signal"]),
             f"손절 {fp['stop_atr']:g}×ATR" if fp.get("stop_atr") else f"손절 {fp['stop']:g}%",
             f"익절 +{fp['take']:g}%"]
    if fp.get("max_hold"):
        parts.append(f"최대 {fp['max_hold']}일")
    if fp.get("market_filter"):
        parts.append("시장 필터")
    if fp.get("core_pct"):
        parts.append(f"코어 {fp['core_pct']}%")
    if fp.get("guard"):
        parts.append("보호")
    ai = {"rules": "규칙만", "consensus": "다수결"}.get(fp["ai"]) or \
        {"claude": "Claude", "gemini": "Gemini", "openai": "ChatGPT"}.get(fp["ai"], fp["ai"])
    if fp["ai"] not in ("rules", "consensus"):
        ai += f"({(fp.get('models') or {}).get(fp['ai'], '')})"
    if fp.get("hybrid"):
        ai += " + 규칙 필터"
    parts.append(ai)
    return " · ".join(parts)


def current(j, cfg) -> int:
    """지금 설정의 버전 번호. 직전 버전과 다르면 새로 만든다 (그 장부 기준)."""
    from .journal import now
    _ensure(j)
    fp = fingerprint(cfg)
    s = json.dumps(fp, sort_keys=True, ensure_ascii=False)
    row = j.db.execute("SELECT id, fp FROM setting_versions WHERE ledger=? ORDER BY id DESC LIMIT 1",
                       (cfg.ledger,)).fetchone()
    if row and row["fp"] == s:
        return row["id"]
    cur = j.db.execute("INSERT INTO setting_versions(ledger, ts, fp, label, entry) VALUES(?,?,?,?,?)",
                       (cfg.ledger, now().isoformat(timespec="seconds"), s, label(fp), entry_kind(fp)))
    j.db.commit()
    return cur.lastrowid


def all_for(j, ledger: str) -> dict[int, dict]:
    """버전 번호 → {id, no(그 장부 안에서 1부터), ts, label, entry, changed(직전과 달라진 것)}"""
    _ensure(j)
    rows = [dict(r) for r in j.db.execute("SELECT * FROM setting_versions WHERE ledger=? ORDER BY id", (ledger,))]
    out, prev = {}, None
    for i, r in enumerate(rows, 1):
        fp = json.loads(r["fp"])
        r["no"] = i
        r["changed"] = diff(prev, fp) if prev else []
        out[r["id"]] = r
        prev = fp
    return out


NAMES = {"signal": "매매 신호", "stop": "손절", "stop_atr": "ATR 손절", "take": "익절", "min_hold": "최소 보유",
         "max_hold": "최대 보유", "market_filter": "시장 필터", "core_pct": "코어 비중", "rule_sell": "규칙 매도",
         "guard": "보호 규칙", "guard_vals": "보호 규칙 값", "late_buy": "돌파 매수 시점", "ai": "AI",
         "hybrid": "규칙 필터", "models": "AI 모델", "min_conf": "최소 확신도", "gemini_thinking": "Gemini 생각 수준"}


def diff(a: dict, b: dict) -> list[str]:
    """직전 버전에서 바뀐 항목 이름들 (한 번에 하나만 바꿨는지 보이게)."""
    return [NAMES.get(k, k) for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)]


def exit_reason(trigger: str | None) -> str:
    """매도 주문의 trigger → 사유 묶음."""
    t = (trigger or "").strip()
    for k in ("손절", "익절", "이익 보호", "보유기간 만료"):
        if t.startswith(k):
            return k
    if t.startswith("규칙 매도"):
        return "돌파 실패" if "돌파 실패" in t else "규칙 매도 신호"
    if t.startswith("AI"):
        return "AI 매도"
    return "직접·기타"
