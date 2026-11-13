"""AI 모델 단가 (100만 토큰당 USD) — 비용·$충전 시 사용 기간 계산용.

목록에 없는 모델이나 단가가 바뀐 모델은 웹 ‘AI 사용량’ 화면에서 직접 고칠 수 있다 (data/ai_cost.json).
조사 시점: 2026-11. 실제 청구액은 각 콘솔에서 확인.
"""
from __future__ import annotations

import json
from pathlib import Path

PRICES: dict[str, tuple[float, float]] = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-3.8-flash": (0.75, 3.75),        # 2027-01-01 부터 두 배 예정
    "gemini-4-hydrogen": (1.50, 7.50),       # 2026-11-12 출시
    "gemini-4-carbon": (4.00, 20.00),        # 출시 할인($2/$10, ~2026-11-30)이 끝난 뒤 기준
    "gemini-4-argon": (4.00, 20.00),         # 출시 할인($2/$10)이 끝난 뒤 기준
    "gpt-6-luna": (0.10, 0.50),
    "gpt-6.1-sol": (2.00, 10.00),
    "gpt-6-astra": (10.00, 50.00),
}
PROVIDER_LABEL = {"claude": "Claude", "gemini": "Gemini", "openai": "ChatGPT"}


def _file(data_dir: Path) -> Path:
    return Path(data_dir) / "ai_cost.json"


def settings(data_dir: Path) -> dict:
    """{"prices": {model: [in, out]}, "free": {"gemini": true}, "credits": {provider: {"amount": 5, "since": "YYYY-MM-DD"}}, "budget": 5}"""
    try:
        d = json.loads(_file(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        d = {}
    d.setdefault("prices", {})
    d.setdefault("free", {"gemini": True})
    d.setdefault("credits", {})
    d.setdefault("budget", 5.0)
    d.setdefault("monthly_limit", 0.0)      # 월 AI 사용 한도 ($, 0 = 없음) — 넘으면 그달은 규칙만으로 실행
    return d


def save(data_dir: Path, d: dict) -> None:
    _file(data_dir).write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


def price(model: str, st: dict) -> tuple[float, float] | None:
    p = st.get("prices", {}).get(model)
    if p and len(p) == 2:
        return float(p[0]), float(p[1])
    return PRICES.get(model)


def cost(provider: str, model: str, tin: int, tout: int, st: dict) -> float | None:
    """USD. 무료 등급이면 0, 단가를 모르면 None."""
    if st.get("free", {}).get(provider):
        return 0.0
    p = price(model, st)
    if p is None:
        return None
    return tin / 1e6 * p[0] + tout / 1e6 * p[1]


def provider_of(model: str) -> str | None:
    m = model.lower()
    if m.startswith("claude"):
        return "claude"
    if m.startswith("gemini"):
        return "gemini"
    if m.startswith(("gpt", "o1", "o3", "o4", "o5", "chatgpt")):
        return "openai"
    return None


def model_options(fields: list, st: dict) -> list:
    """설정 화면 모델 목록(MODEL_FIELDS)에 단가표에서 직접 추가한 모델을 덧붙인다."""
    out = []
    for key, label, opts in fields:
        prov = key.split("_")[0]
        have = {v for v, _ in opts}
        extra = [(m, f"{m} · 직접 추가") for m in sorted(st.get("prices", {})) if provider_of(m) == prov and m not in have]
        out.append((key, label, list(opts) + extra))
    return out


def ai_cost(j, data_dir, ledger: str | None, start=None, end=None) -> dict:
    """기간 AI 비용 (USD). ledger 를 주면 그 장부(시장) 실행에서 쓴 것만. 반환: {usd, calls, unknown}"""
    st = settings(data_dir)
    usd, calls, unknown = 0.0, 0, 0
    a = start.isoformat() if start else None
    b = end.isoformat() if end else None
    for r in j.usage(a):
        if b and r["ts"][:10] > b:
            continue
        if ledger and r.get("ledger") and r["ledger"] != ledger:
            continue
        calls += 1
        c = cost(r["provider"], r["model"], r["tin"], r["tout"], st)
        if c is None:
            unknown += 1
        else:
            usd += c
    return {"usd": round(usd, 4), "calls": calls, "unknown": unknown}


def month_status(j, data_dir) -> dict:
    """이번 달(한국 날짜) AI 비용과 한도. {spend, limit, over, month}"""
    from .journal import now
    first = now().date().replace(day=1)
    spend = ai_cost(j, data_dir, None, first)["usd"]
    limit = float(settings(data_dir).get("monthly_limit") or 0)
    return {"spend": round(spend, 4), "limit": limit, "over": bool(limit) and spend >= limit,
            "month": first.strftime("%Y-%m")}
