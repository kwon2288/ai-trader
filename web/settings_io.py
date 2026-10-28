"""config.toml 의 주석을 보존하면서 값만 바꿔 쓰기."""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

# (섹션, 키, 타입, 최소, 최대, 라벨)
FIELDS = [
    ("budget", "total_krw", int, 10_000, 100_000_000, "봇 총 원금 (원)"),
    ("budget", "max_per_position_krw", int, 1_000, 100_000_000, "종목당 최대 금액 (원)"),
    ("budget", "max_positions", int, 1, 10, "최대 보유 종목 수"),
    ("risk", "stop_loss_pct", float, -50, -0.5, "손절 (%)"),
    ("risk", "take_profit_pct", float, 0.5, 200, "익절 (%)"),
    ("risk", "max_drawdown_pct", float, -90, -1, "최대 손실 시 정지 (%)"),
    ("risk", "max_orders_per_day", int, 0, 50, "하루 최대 주문 수"),
    ("risk", "min_confidence", float, 0, 1, "최소 AI 확신도 (0~1)"),
    ("risk", "min_hold_days", int, 0, 60, "최소 보유일"),
]
AUTO_FIELDS = [
    ("universe", "auto_count", int, 0, 30, "자동 선정 종목 수 (0 = 끄기)"),
    ("universe", "auto_min_cap_eok", int, 0, 10_000_000, "최소 시가총액 (억원)"),
    ("universe", "auto_min_price", int, 0, 1_000_000, "최소 주가 (원)"),
    ("universe", "auto_scan_pool", int, 20, 1000, "시세 조회 범위 (시총 상위 N개)"),
]
DEFAULTS = {"auto_count": 10, "auto_min_cap_eok": 3000, "auto_min_price": 1000, "auto_scan_pool": 200}
ORDER_TYPES = {"market": "시장가", "limit": "지정가"}
PROVIDER_CHOICES = {
    "claude": "Claude",
    "gemini": "Gemini",
    "openai": "ChatGPT",
    "consensus": "다수결 (키가 있는 AI 모두에게 묻고 과반 의견만 실행)",
}
MODEL_FIELDS = [  # (설정키, 라벨, 추천 목록)
    ("claude_model", "Claude 모델", [
        ("claude-sonnet-5-5", "Sonnet 5.5 · 기본"),
        ("claude-haiku-5-5", "Haiku 5.5 · 저렴"),
        ("claude-opus-5-5", "Opus 5.5 · 고성능"),
    ]),
    ("gemini_model", "Gemini 모델", [
        ("gemini-3.8-flash", "3.8 Flash · 기본 (무료 ~20회/일)"),
        ("gemini-3.5-flash-lite", "3.5 Flash-Lite · 저렴 (무료 ~500회/일)"),
        ("gemini-4-argon", "4 Argon · 고성능"),
    ]),
    ("openai_model", "ChatGPT 모델", [
        ("gpt-6.1-sol", "GPT-6.1 Sol · 기본"),
        ("gpt-6-luna", "GPT-6 Luna · 저렴"),
        ("gpt-6-astra", "GPT-6 Astra · 고성능"),
    ]),
]


def read(path: Path) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _fmt(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return f"{v:_}" if abs(v) >= 10_000 else str(v)
    if isinstance(v, float):
        return repr(round(v, 4))
    return '"' + str(v).replace('"', "") + '"'


def _set(text: str, section: str, key: str, value) -> str:
    """[section] 블록 안의 `key = ...` 줄 값만 교체 (줄 끝 주석 유지)."""
    sec = re.search(rf"^\[{re.escape(section)}\]\s*$", text, re.M)
    if not sec:
        return text.rstrip() + f"\n\n[{section}]\n{key} = {_fmt(value)}\n"
    nxt = re.search(r"^\[", text[sec.end():], re.M)
    end = sec.end() + (nxt.start() if nxt else len(text) - sec.end())
    block = text[sec.end():end]
    pat = re.compile(rf"^(\s*{re.escape(key)}\s*=\s*)([^#\n]*?)(\s*(#.*)?)$", re.M)
    if pat.search(block):
        block = pat.sub(lambda m: m.group(1) + _fmt(value) + m.group(3), block, count=1)
    else:
        block = block.rstrip("\n") + f"\n{key} = {_fmt(value)}\n"
    return text[:sec.end()] + block + text[end:]


def _set_symbols(text: str, symbols: list[str]) -> str:
    body = "symbols = [\n" + "".join(f'  "{s}",\n' for s in symbols) + "]"
    new, n = re.subn(r"^symbols\s*=\s*\[.*?\]", body, text, count=1, flags=re.M | re.S)
    return new if n else text.rstrip() + f"\n\n[universe]\n{body}\n"


def validate_and_write(path: Path, form: dict) -> list[str]:
    """폼 값을 검증해 저장. 오류 메시지 목록 반환 (비어 있으면 성공)."""
    errors, values = [], {}
    for sec, key, typ, lo, hi, label in FIELDS + AUTO_FIELDS:
        raw = str(form.get(key, "")).replace(",", "").replace("_", "").strip()
        try:
            v = typ(float(raw)) if typ is int else typ(raw)
        except ValueError:
            errors.append(f"{label}: 숫자를 입력하세요")
            continue
        if not lo <= v <= hi:
            errors.append(f"{label}: {lo} ~ {hi} 범위여야 합니다")
            continue
        values[(sec, key)] = v
    if not errors and values[("budget", "max_per_position_krw")] > values[("budget", "total_krw")]:
        errors.append("종목당 최대 금액이 총 원금보다 클 수 없습니다")

    order_type = form.get("order_type", "market")
    if order_type not in ORDER_TYPES:
        errors.append("주문 방식이 올바르지 않습니다")
    provider = form.get("provider", "claude")
    if provider not in PROVIDER_CHOICES:
        errors.append("AI 선택이 올바르지 않습니다")
    models = {}
    for key, label, _ in MODEL_FIELDS:
        m = form.get(key, "").strip()
        if m == "__custom__":   # 드롭다운에서 '직접 입력' 선택
            m = form.get(f"{key}_custom", "").strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9.\-]{2,60}", m):
            errors.append(f"{label} 이름이 올바르지 않습니다")
        models[key] = m

    symbols = [s.strip().zfill(6) for s in re.split(r"[\s,]+", form.get("symbols", "")) if s.strip()]
    bad = [s for s in symbols if not re.fullmatch(r"[0-9A-Z]{6}", s)]
    if bad:
        errors.append(f"종목코드 형식 오류: {', '.join(bad)}")
    if not symbols:
        errors.append("종목을 1개 이상 입력하세요")
    if len(symbols) > 40:
        errors.append("종목은 40개까지만 넣을 수 있습니다 (조회 시간·비용)")
    if errors:
        return errors

    text = path.read_text(encoding="utf-8")
    for (sec, key), v in values.items():
        text = _set(text, sec, key, v)
    text = _set(text, "order", "type", order_type)
    text = _set(text, "ai", "provider", provider)
    for key, m in models.items():
        text = _set(text, "ai", key, m)
    # 예전 설정의 model 키 제거 (claude_model 로 대체)
    text = re.sub(r"^model\s*=.*\n", "", text, flags=re.M)
    text = _set_symbols(text, list(dict.fromkeys(symbols)))
    text = _set(text, "universe", "auto_include_etf", form.get("auto_include_etf") == "on")
    tomllib.loads(text)  # 깨진 파일은 저장하지 않음
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return []
