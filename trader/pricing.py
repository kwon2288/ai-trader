"""AI 모델 단가 (100만 토큰당 USD) — 비용·$충전 시 사용 기간 계산용.

목록에 없는 모델이나 단가가 바뀐 모델은 웹 ‘AI 사용량’ 화면에서 직접 고칠 수 있다 (data/ai_cost.json).
조사 시점: 2026-10. 실제 청구액은 각 콘솔에서 확인.
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
