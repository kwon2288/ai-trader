"""AI 분석기 — Claude / Gemini / ChatGPT 중 선택, 또는 여러 AI 다수결(consensus).

AI 는 '제안'만 한다. 실제 주문 여부·수량은 bot.py 의 위험관리 규칙이 결정한다.
어느 AI 든 같은 데이터·같은 지침을 받고, 같은 형식(JSON)으로 답한다.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from collections import Counter

import requests

log = logging.getLogger(__name__)

PROVIDERS = {
    "claude": {"label": "Claude", "env": "ANTHROPIC_API_KEY", "default": "claude-sonnet-5-5"},
    "gemini": {"label": "Gemini", "env": "GEMINI_API_KEY", "default": "gemini-3.8-flash"},
    "openai": {"label": "ChatGPT", "env": "OPENAI_API_KEY", "default": "gpt-6.1-sol"},
}

SYSTEM = """당신은 신중한 소액 주식 포트폴리오 매니저입니다.
데이터의 market 이 KR 이면 한국(KRX), US 면 미국 주식이며, 모든 금액·가격은 currency 기준입니다.
주어진 데이터만으로 판단하고, 데이터에 없는 사실(뉴스·실적 등)을 지어내지 마세요.

원칙:
- 원금 보전이 최우선입니다. 확신이 없으면 HOLD 가 정답입니다.
- 매매마다 수수료·세금이 들므로 잦은 교체를 피하세요. 직전 판단을 뒤집으려면 분명한 근거가 있어야 합니다.
- 추세(이동평균 정배열, 20/60일 수익률)와 과열/과매도(RSI), 변동성을 함께 보세요.
- 경고(투자유의·단기과열 등)가 붙은 종목은 사지 마세요.
- 이미 보유 중인 종목은 계속 보유할 이유가 약해졌는지 평가하세요.
- 손절·익절은 시스템이 자동으로 처리하므로, 당신은 추세 판단에 집중하세요.
- 후보의 source 는 core(사용자가 고른 종목), auto(오늘 거래대금 상위로 자동 선정), held(보유 중)입니다.
  auto 종목은 거래가 활발하다는 뜻일 뿐 좋은 종목이라는 뜻이 아니니, 지표로만 판단하세요.
- rule_signal 이 있으면 규칙이 1차로 고른 매수/매도 후보입니다. 그대로 따르지 말고 지표를 다시 보고
  확인되면 그 행동을, 근거가 약하면 HOLD 를 내세요. (후보 목록에 있는 종목만 판단하세요)
- confidence 는 0~1 사이로, 근거가 강할 때만 0.7 이상을 주세요.
- reason 은 한국어 한두 문장으로, 어떤 지표 때문인지 구체적으로 쓰세요."""

DECISION_PROPS = {
    "code": {"type": "string", "description": "종목코드 — 한국은 6자리, 미국은 티커 (후보 목록 안에서만)"},
    "action": {"type": "string", "enum": ["BUY", "SELL", "HOLD"]},
    "amount_krw": {"type": "number", "description": "BUY 일 때 투입할 금액(currency 단위), 아니면 0"},
    "sell_ratio": {"type": "number", "description": "SELL 일 때 보유 수량 중 매도 비율 0~1, 아니면 0"},
    "confidence": {"type": "number", "description": "0~1"},
    "reason": {"type": "string"},
}
SCHEMA = {
    "type": "object",
    "properties": {
        "market_view": {"type": "string", "description": "오늘 시장/후보군에 대한 한두 문장 요약"},
        "decisions": {
            "type": "array",
            "items": {"type": "object", "properties": DECISION_PROPS,
                      "required": list(DECISION_PROPS), "additionalProperties": False},
        },
    },
    "required": ["market_view", "decisions"],
    "additionalProperties": False,
}

JSON_HINT = """
반드시 아래 형식의 JSON 객체 하나로만 답하세요 (설명 문장·코드블록 없이):
{"market_view": "...", "decisions": [{"code": "005930", "action": "BUY|SELL|HOLD",
  "amount_krw": 0, "sell_ratio": 0, "confidence": 0.0, "reason": "..."}]}"""


def build_prompt(ctx: dict) -> str:
    return (
        "아래는 현재 포트폴리오와 후보 종목 데이터입니다 (JSON).\n"
        "보유 종목마다 HOLD/SELL 을, 새로 살 만한 종목이 있으면 BUY 를 제출하세요. "
        "살 종목이 없으면 BUY 를 내지 마세요.\n\n"
        f"```json\n{json.dumps(ctx, ensure_ascii=False, indent=1)}\n```"
    )


# 토큰 사용 기록 — runner 가 일지에 쌓도록 연결한다 (provider, model, 입력, 출력)
USAGE_SINK = None


def _record(provider: str, model: str, usage: dict | None) -> None:
    if USAGE_SINK and usage:
        try:
            USAGE_SINK(provider, model, int(usage.get("in", 0)), int(usage.get("out", 0)))
        except Exception:  # noqa: BLE001
            pass


def _extract_json(text: str) -> dict:
    """코드블록·앞뒤 설명이 섞여 와도 첫 JSON 객체를 꺼낸다."""
    text = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if m:
        text = m.group(1)
    try:
        return json.loads(text)
    except ValueError:
        a, b = text.find("{"), text.rfind("}")
        if a >= 0 and b > a:
            return json.loads(text[a:b + 1])
        raise RuntimeError(f"AI 응답이 JSON 이 아닙니다: {text[:200]}") from None


def normalize(raw: dict, provider: str) -> dict:
    """어느 AI 가 답했든 같은 모양·같은 범위로 정리 (이상한 값은 버림)."""
    out = []
    for d in raw.get("decisions") or []:
        if not isinstance(d, dict):
            continue
        action = str(d.get("action", "")).upper().strip()
        code = re.sub(r"[^A-Z0-9.\-]", "", str(d.get("code", "")).strip().upper())
        if action not in {"BUY", "SELL", "HOLD"} or not code:
            continue
        try:
            conf = min(max(float(d.get("confidence") or 0), 0.0), 1.0)
            amount = max(round(float(d.get("amount_krw") or 0), 2), 0)
            ratio = min(max(float(d.get("sell_ratio") or 0), 0.0), 1.0)
        except (TypeError, ValueError):
            continue
        out.append({"code": code.zfill(6) if code.isdigit() else code, "action": action, "amount_krw": amount,
                    "sell_ratio": ratio, "confidence": conf, "reason": str(d.get("reason", "")).strip()})
    return {"market_view": str(raw.get("market_view", "")).strip(), "decisions": out,
            "_provider": provider, "_usage": raw.get("_usage")}


RETRY_STATUS = {429, 500, 502, 503, 504}
RETRY_WAITS = [10, 30, 60]   # 초 — 서버 혼잡(503)·한도(429)는 대개 잠깐이면 풀린다


def post_with_retry(url: str, **kw) -> requests.Response:
    """일시적 오류(혼잡·한도·서버오류·네트워크)는 기다렸다가 최대 3번 더 시도."""
    for i, wait in enumerate([0] + RETRY_WAITS):
        if wait:
            log.info("AI 서버 일시 오류 → %s초 후 재시도 (%d/%d)", wait, i, len(RETRY_WAITS))
            time.sleep(wait)
        try:
            r = requests.post(url, **kw)
        except (requests.ConnectionError, requests.Timeout):
            if i == len(RETRY_WAITS):
                raise
            continue
        if r.status_code not in RETRY_STATUS or i == len(RETRY_WAITS):
            return r
        ra = r.headers.get("retry-after") if hasattr(r, "headers") else None
        if ra and ra.isdigit() and int(ra) <= 120:
            time.sleep(int(ra))
    return r


# ── 개별 AI ──────────────────────────────────────────────
class ClaudeAnalyst:
    name = "claude"

    def __init__(self, model: str, max_tokens: int = 4000):
        import anthropic
        self.client = anthropic.Anthropic(max_retries=4)  # ANTHROPIC_API_KEY, 혼잡·한도 시 SDK가 자동 재시도
        self.model, self.max_tokens = model, max_tokens
        self.label = f"Claude({model})"

    # 일부 모델은 도구 강제 호출(tool_choice=tool/any)을 지원하지 않는다 → 그때는 auto + 지시문으로 다시 요청
    _no_forced: set = set()

    def _create(self, ctx: dict, forced: bool):
        tool = {"name": "submit_decisions", "description": "포트폴리오 매매 판단을 제출한다.", "input_schema": SCHEMA}
        system = SYSTEM if forced else (SYSTEM + "\n\n반드시 submit_decisions 도구를 한 번 호출해 판단을 제출하세요. "
                                                 "도구를 쓸 수 없으면 같은 형식의 JSON만 출력하세요.")
        return self.client.messages.create(
            model=self.model, max_tokens=self.max_tokens, system=system, tools=[tool],
            tool_choice={"type": "tool", "name": "submit_decisions"} if forced else {"type": "auto"},
            messages=[{"role": "user", "content": build_prompt(ctx)}],
        )

    def decide(self, ctx: dict) -> dict:
        forced = self.model not in self._no_forced
        try:
            msg = self._create(ctx, forced)
        except Exception as e:  # noqa: BLE001
            if forced and "tool_choice" in str(e):
                ClaudeAnalyst._no_forced.add(self.model)
                msg = self._create(ctx, False)
            else:
                raise
        u = getattr(msg, "usage", None)
        if u:
            _record("claude", self.model, {"in": u.input_tokens, "out": u.output_tokens})
        raw = None
        for block in msg.content:
            if getattr(block, "type", "") == "tool_use":
                raw = dict(block.input)
                break
        if raw is None:                                   # 도구 대신 글로 JSON 을 준 경우
            text = "".join(getattr(b, "text", "") for b in msg.content)
            try:
                raw = _extract_json(text)
            except Exception:  # noqa: BLE001
                raise RuntimeError("Claude 응답에 판단이 없습니다") from None
        u = getattr(msg, "usage", None)
        if u:
            raw["_usage"] = {"in": u.input_tokens, "out": u.output_tokens}
        return normalize(raw, self.label)


class GeminiAnalyst:
    """Gemini REST (generateContent, JSON 모드). 키: GEMINI_API_KEY"""
    name = "gemini"
    URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    def __init__(self, model: str, max_tokens: int = 4000):
        self.key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or ""
        if not self.key:
            raise RuntimeError(".env 에 GEMINI_API_KEY 가 없습니다")
        self.model, self.max_tokens = model, max_tokens
        self.label = f"Gemini({model})"

    def decide(self, ctx: dict) -> dict:
        body = {
            "systemInstruction": {"parts": [{"text": SYSTEM + JSON_HINT}]},
            "contents": [{"role": "user", "parts": [{"text": build_prompt(ctx)}]}],
            "generationConfig": {"responseMimeType": "application/json",
                                 "maxOutputTokens": max(self.max_tokens, 8000)},
        }
        r = post_with_retry(self.URL.format(model=self.model), json=body, timeout=180,
                          headers={"x-goog-api-key": self.key, "Content-Type": "application/json"})
        data = r.json() if r.content else {}
        if r.status_code != 200:
            raise RuntimeError(f"Gemini 오류 {r.status_code}: {(data.get('error') or {}).get('message', r.text[:200])}")
        cands = data.get("candidates") or []
        if not cands:
            raise RuntimeError(f"Gemini 응답 없음 (차단 사유: {(data.get('promptFeedback') or {}).get('blockReason', '-')})")
        text = "".join(p.get("text", "") for p in (cands[0].get("content") or {}).get("parts", [])
                       if not p.get("thought"))
        u = data.get("usageMetadata") or {}
        usage = {"in": u.get("promptTokenCount", 0),
                 "out": u.get("candidatesTokenCount", 0) + u.get("thoughtsTokenCount", 0)}  # 생각 토큰도 출력 요금
        _record("gemini", self.model, usage)                # 답을 못 읽어도 요금은 나가므로 먼저 기록
        raw = _extract_json(text)
        raw["_usage"] = usage
        return normalize(raw, self.label)


class OpenAIAnalyst:
    """OpenAI Responses API + Structured Outputs(JSON Schema). 키: OPENAI_API_KEY"""
    name = "openai"
    URL = "https://api.openai.com/v1/responses"

    def __init__(self, model: str, max_tokens: int = 4000):
        self.key = os.environ.get("OPENAI_API_KEY", "")
        if not self.key:
            raise RuntimeError(".env 에 OPENAI_API_KEY 가 없습니다")
        self.model, self.max_tokens = model, max_tokens
        self.label = f"ChatGPT({model})"

    def _post(self, ctx: dict, fmt: dict) -> requests.Response:
        return post_with_retry(self.URL, timeout=240, headers={"Authorization": f"Bearer {self.key}"}, json={
            "model": self.model,
            "instructions": SYSTEM + ("" if fmt.get("type") == "json_schema" else JSON_HINT),
            "input": build_prompt(ctx),
            "text": {"format": fmt},
            # 추론 모델은 생각에도 토큰을 쓰므로 넉넉히
            "max_output_tokens": max(self.max_tokens * 4, 16000),
        })

    def decide(self, ctx: dict) -> dict:
        r = self._post(ctx, {"type": "json_schema", "name": "decisions", "schema": SCHEMA, "strict": True})
        if r.status_code == 400:  # 구조화 출력 미지원 모델 → 일반 JSON 모드로 재시도
            log.info("OpenAI json_schema 거부, json_object 로 재시도: %s", r.text[:200])
            r = self._post(ctx, {"type": "json_object"})
        data = r.json() if r.content else {}
        if r.status_code != 200:
            raise RuntimeError(f"OpenAI 오류 {r.status_code}: {(data.get('error') or {}).get('message', r.text[:200])}")
        text = data.get("output_text") or "".join(
            c.get("text", "") for item in data.get("output") or [] if item.get("type") == "message"
            for c in item.get("content") or [] if c.get("type") == "output_text")
        u = data.get("usage") or {}
        usage = {"in": u.get("input_tokens", 0), "out": u.get("output_tokens", 0)}
        _record("openai", self.model, usage)
        if not text:
            raise RuntimeError(f"OpenAI 응답에 본문이 없습니다 (status={data.get('status')}, "
                               f"사유={(data.get('incomplete_details') or {}).get('reason', '-')})")
        raw = _extract_json(text)
        raw["_usage"] = usage
        return normalize(raw, self.label)


# ── 다수결 ──────────────────────────────────────────────
def merge_votes(results: list[dict]) -> dict:
    """여러 AI 결과를 종목별 다수결로 합친다.

    - 응답한 AI 의 과반이 같은 행동(BUY/SELL)을 낼 때만 그 행동을 채택, 아니면 HOLD.
    - 확신도는 찬성한 AI 들의 평균, 금액·매도비율은 찬성 AI 중 가장 보수적인 값.
    """
    n = len(results)
    need = n // 2 + 1
    by_code: dict[str, list[tuple[str, dict]]] = {}
    for res in results:
        for d in res["decisions"]:
            by_code.setdefault(d["code"], []).append((res["_provider"], d))

    merged = []
    for code, votes in by_code.items():
        counts = Counter(d["action"] for _, d in votes)
        action, cnt = counts.most_common(1)[0]
        tally = " / ".join(f"{p.split('(')[0]}:{d['action']}" for p, d in votes)
        if action in {"BUY", "SELL"} and cnt >= need:
            agree = [d for _, d in votes if d["action"] == action]
            merged.append({
                "code": code, "action": action,
                "amount_krw": min((d["amount_krw"] for d in agree if d["amount_krw"]), default=0),
                "sell_ratio": min((d["sell_ratio"] for d in agree if d["sell_ratio"]), default=1.0),
                "confidence": round(sum(d["confidence"] for d in agree) / len(agree), 2),
                "reason": f"[다수결 {cnt}/{n} · {tally}] " + " | ".join(
                    f"{p.split('(')[0]}: {d['reason']}" for p, d in votes if d["action"] == action),
            })
        else:
            avg = [d["confidence"] for _, d in votes if d["action"] == "HOLD"] or [0.5]
            merged.append({
                "code": code, "action": "HOLD", "amount_krw": 0, "sell_ratio": 0,
                "confidence": round(sum(avg) / len(avg), 2),
                "reason": f"[의견 불일치 → 유지 · {tally}]" if len(counts) > 1 else
                          f"[{tally}] " + " | ".join(f"{p.split('(')[0]}: {d['reason']}" for p, d in votes),
            })
    usage = {"in": sum((r.get("_usage") or {}).get("in", 0) for r in results),
             "out": sum((r.get("_usage") or {}).get("out", 0) for r in results)}
    return {
        "market_view": " / ".join(f"{r['_provider'].split('(')[0]}: {r['market_view']}" for r in results),
        "decisions": merged, "_provider": "다수결(" + ", ".join(r["_provider"].split("(")[0] for r in results) + ")",
        "_usage": usage,
    }


class ConsensusAnalyst:
    """키가 있는 AI 들에게 모두 묻고 다수결. 2개 이상 응답해야 판단을 낸다."""
    name = "consensus"

    def __init__(self, members: list):
        if len(members) < 2:
            raise RuntimeError("다수결 모드는 AI 키가 2개 이상 필요합니다 (ANTHROPIC/GEMINI/OPENAI_API_KEY)")
        self.members = members
        self.label = "다수결(" + ", ".join(m.label for m in members) + ")"
        self.errors: list[str] = []

    def decide(self, ctx: dict) -> dict:
        from concurrent.futures import ThreadPoolExecutor
        self.errors = []
        results = []
        with ThreadPoolExecutor(max_workers=len(self.members)) as ex:
            futs = {ex.submit(m.decide, ctx): m for m in self.members}
            for f, m in futs.items():
                try:
                    results.append(f.result())
                except Exception as e:  # noqa: BLE001
                    self.errors.append(f"{m.label}: {e}")
        if len(results) < 2:
            raise RuntimeError("다수결에 필요한 응답이 2개 미만입니다 → " + "; ".join(self.errors))
        out = merge_votes(results)
        if self.errors:
            out["market_view"] += f" (응답 실패: {'; '.join(self.errors)})"
        return out


# ── 생성 ────────────────────────────────────────────────
_CLASSES = {"claude": ClaudeAnalyst, "gemini": GeminiAnalyst, "openai": OpenAIAnalyst}


def available(provider: str) -> bool:
    p = PROVIDERS[provider]
    return bool(os.environ.get(p["env"]) or (provider == "gemini" and os.environ.get("GOOGLE_API_KEY")))


def make_analyst(cfg):
    prov = cfg.ai_provider
    if prov == "rules":
        from .signals import RulesAnalyst
        return RulesAnalyst()
    if prov == "consensus":
        members = [_CLASSES[p](cfg.ai_models[p], cfg.ai_max_tokens) for p in PROVIDERS if available(p)]
        return ConsensusAnalyst(members)
    if prov not in _CLASSES:
        raise RuntimeError(f"알 수 없는 AI: {prov} (claude/gemini/openai/consensus/rules)")
    if not available(prov):
        raise RuntimeError(f".env 에 {PROVIDERS[prov]['env']} 가 없습니다 ({PROVIDERS[prov]['label']} 사용 시 필요)")
    return _CLASSES[prov](cfg.ai_models[prov], cfg.ai_max_tokens)
