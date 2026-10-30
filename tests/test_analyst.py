"""AI 분석기 오프라인 테스트 — Gemini/ChatGPT HTTP 응답을 흉내 내고, 다수결 규칙을 검증한다."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trader import analyst as A  # noqa: E402


class R:
    def __init__(self, body, status=200):
        self._b, self.status_code = body, status
        self.content, self.text = b"x", json.dumps(body, ensure_ascii=False)
        self.headers = {}

    def json(self):
        return self._b


ANSWER = {"market_view": "횡보", "decisions": [
    {"code": "69500", "action": "buy", "amount_krw": "40000", "sell_ratio": 0, "confidence": 1.4, "reason": "정배열"},
    {"code": "005930", "action": "WAIT", "confidence": 0.9, "reason": "이상한 행동 → 버려져야 함"},
]}
CTX = {"candidates": []}


def test_gemini_parses_codeblock_and_normalizes():
    os.environ["GEMINI_API_KEY"] = "g"
    sent = {}

    def fake_post(url, json=None, headers=None, timeout=None):  # noqa: A002
        sent.update(url=url, body=json, headers=headers)
        text = "설명입니다\n```json\n" + __import__("json").dumps(ANSWER, ensure_ascii=False) + "\n```"
        return R({"candidates": [{"content": {"parts": [{"text": "생각중", "thought": True}, {"text": text}]}}],
                  "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 20}})

    A.requests.post = fake_post
    r = A.GeminiAnalyst("gemini-3.8-flash").decide(CTX)
    assert "gemini-3.8-flash:generateContent" in sent["url"] and sent["headers"]["x-goog-api-key"] == "g"
    assert sent["body"]["generationConfig"]["responseMimeType"] == "application/json"
    assert r["decisions"] == [{"code": "069500", "action": "BUY", "amount_krw": 40000, "sell_ratio": 0.0,
                               "confidence": 1.0, "reason": "정배열"}]
    assert r["_usage"] == {"in": 100, "out": 20} and r["_provider"].startswith("Gemini")


def test_gemini_retries_on_503():
    os.environ["GEMINI_API_KEY"] = "g"
    A.RETRY_WAITS[:] = [0, 0, 0]
    n = {"c": 0}

    def fake_post(url, json=None, headers=None, timeout=None):  # noqa: A002
        n["c"] += 1
        if n["c"] < 3:
            return R({"error": {"message": "high demand"}}, 503)
        return R({"candidates": [{"content": {"parts": [{"text": __import__("json").dumps(ANSWER)}]}}]})

    A.requests.post = fake_post
    r = A.GeminiAnalyst("gemini-3.8-flash").decide(CTX)
    assert n["c"] == 3 and r["decisions"][0]["action"] == "BUY"

    n["c"] = -10   # 계속 503 → 4번 시도 후 오류
    try:
        A.GeminiAnalyst("gemini-3.8-flash").decide(CTX)
    except RuntimeError as e:
        assert "503" in str(e) and n["c"] == -6
    else:
        raise AssertionError


def test_openai_schema_then_fallback():
    os.environ["OPENAI_API_KEY"] = "o"
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):  # noqa: A002
        calls.append(json["text"]["format"]["type"])
        if json["text"]["format"]["type"] == "json_schema":
            return R({"error": {"message": "json_schema not supported"}}, 400)
        return R({"output": [{"type": "reasoning"}, {"type": "message", "content": [
            {"type": "output_text", "text": __import__("json").dumps(ANSWER)}]}],
            "usage": {"input_tokens": 50, "output_tokens": 10}})

    A.requests.post = fake_post
    r = A.OpenAIAnalyst("gpt-6.1-sol").decide(CTX)
    assert calls == ["json_schema", "json_object"]
    assert r["decisions"][0]["action"] == "BUY" and len(r["decisions"]) == 1


def test_openai_error_message():
    A.requests.post = lambda *a, **k: R({"error": {"message": "model not found"}}, 404)
    try:
        A.OpenAIAnalyst("nope").decide(CTX)
    except RuntimeError as e:
        assert "model not found" in str(e)
        return
    raise AssertionError


def _res(provider, *ds):
    return {"_provider": f"{provider}(m)", "market_view": provider, "_usage": {"in": 1, "out": 1},
            "decisions": [dict(code=c, action=a, amount_krw=amt, sell_ratio=0, confidence=cf, reason=provider)
                          for c, a, amt, cf in ds]}


def test_consensus_majority_rules():
    out = A.merge_votes([
        _res("Claude", ("000001", "BUY", 40000, 0.8), ("000002", "SELL", 0, 0.9), ("000003", "BUY", 30000, 0.9)),
        _res("Gemini", ("000001", "BUY", 30000, 0.6), ("000002", "HOLD", 0, 0.5), ("000003", "HOLD", 0, 0.6)),
        _res("ChatGPT", ("000001", "HOLD", 0, 0.5), ("000002", "SELL", 0, 0.7), ("000003", "SELL", 0, 0.7)),
    ])
    d = {x["code"]: x for x in out["decisions"]}
    assert d["000001"]["action"] == "BUY" and d["000001"]["amount_krw"] == 30000   # 2/3 찬성, 보수적 금액
    assert d["000001"]["confidence"] == 0.7                                          # (0.8+0.6)/2
    assert d["000002"]["action"] == "SELL"                                           # 2/3 매도
    assert d["000003"]["action"] == "HOLD"                                           # 1:1:1 → 유지
    assert "다수결" in out["_provider"] and out["_usage"] == {"in": 3, "out": 3}


def test_consensus_two_ais_need_both():
    out = A.merge_votes([_res("Claude", ("000001", "BUY", 40000, 0.9)),
                         _res("Gemini", ("000001", "HOLD", 0, 0.5))])
    assert out["decisions"][0]["action"] == "HOLD"   # 2개 중 1개만 매수 → 과반 아님


def test_consensus_tolerates_one_failure():
    class Ok:
        def __init__(self, n, act):
            self.label, self.n, self.act = f"{n}(m)", n, act

        def decide(self, ctx):
            return _res(self.n, ("000001", self.act, 40000, 0.8))

    class Bad:
        label = "ChatGPT(m)"

        def decide(self, ctx):
            raise RuntimeError("timeout")

    c = A.ConsensusAnalyst([Ok("Claude", "BUY"), Ok("Gemini", "BUY"), Bad()])
    out = c.decide(CTX)
    assert out["decisions"][0]["action"] == "BUY" and "응답 실패" in out["market_view"]
    try:
        A.ConsensusAnalyst([Ok("Claude", "BUY"), Bad(), Bad()]).decide(CTX)
    except RuntimeError:
        pass
    else:
        raise AssertionError("응답 1개로는 판단하면 안 됨")


def test_make_analyst_requires_key():
    for k in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY"):
        os.environ.pop(k, None)
    cfg = SimpleNamespace(ai_provider="openai", ai_models={"openai": "x"}, ai_max_tokens=100)
    try:
        A.make_analyst(cfg)
    except RuntimeError as e:
        assert "OPENAI_API_KEY" in str(e)
    else:
        raise AssertionError
    os.environ["GEMINI_API_KEY"] = "g"
    cfg.ai_provider, cfg.ai_models = "consensus", {"claude": "a", "gemini": "b", "openai": "c"}
    try:
        A.make_analyst(cfg)   # 키 1개뿐 → 다수결 불가
    except RuntimeError as e:
        assert "2개 이상" in str(e)
    else:
        raise AssertionError


def test_claude_falls_back_when_forced_tool_unsupported():
    import types
    from trader import analyst as A
    calls = []

    class Msgs:
        def create(self, **kw):
            calls.append(kw["tool_choice"]["type"])
            if kw["tool_choice"]["type"] == "tool":
                raise RuntimeError("Error code: 400 - tool_choice: type \"tool\" and \"any\" are not supported for this model.")
            text = '판단입니다 ```json\n{"market_view": "보합", "decisions": [{"code": "5930", "action": "HOLD", "confidence": 0.7, "reason": "r"}]}\n```'
            return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text=text)],
                                         usage=types.SimpleNamespace(input_tokens=10, output_tokens=5))

    a = A.ClaudeAnalyst.__new__(A.ClaudeAnalyst)
    a.client = types.SimpleNamespace(messages=Msgs())
    a.model, a.max_tokens, a.label = "claude-test-x", 100, "Claude(test)"
    r = a.decide({"candidates": [], "portfolio": {}, "rules": {}})
    assert calls == ["tool", "auto"] and r["decisions"][0]["code"] == "005930"
    a.decide({"candidates": [], "portfolio": {}, "rules": {}})
    assert calls == ["tool", "auto", "auto"]                 # 다음부터는 바로 auto


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
