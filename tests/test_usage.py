"""AI 사용량·비용 — 기록, 단가, $충전 시 기간, 잔액."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.update(TRADER_DATA_DIR=tempfile.mkdtemp(), WEB_PASSWORD="secret-pass", TRADING_MODE="dry",
                  ANTHROPIC_API_KEY="x", GEMINI_API_KEY="y")

from trader import analyst, config as cfgmod, pricing  # noqa: E402
from trader.journal import Journal  # noqa: E402


def test_cost_and_free():
    st = {"prices": {}, "free": {"gemini": True}, "credits": {}, "budget": 5}
    assert abs(pricing.cost("claude", "claude-sonnet-5-5", 1_000_000, 100_000, st) - 3.0) < 1e-9
    assert pricing.cost("gemini", "gemini-3.8-flash", 10**6, 10**6, st) == 0.0
    assert pricing.cost("openai", "unknown-model", 1, 1, st) is None
    st["prices"]["unknown-model"] = [1, 2]
    assert abs(pricing.cost("openai", "unknown-model", 10**6, 10**6, st) - 3.0) < 1e-9


def test_sink_records_and_page():
    c = cfgmod.load()
    j = Journal(c.db_path)
    analyst.USAGE_SINK = lambda p, m, ti, to: j.add_usage("mock", p, m, ti, to, "timer")
    for _ in range(4):
        analyst._record("claude", "claude-sonnet-5-5", {"in": 2000, "out": 300})   # 회당 $0.007
    analyst._record("gemini", "gemini-3.8-flash", {"in": 2000, "out": 300})
    analyst.USAGE_SINK = None
    pricing.save(c.data_dir, {"budget": 5, "free": {"gemini": True},
                              "credits": {"claude": {"amount": 5, "since": "2026-01-01"}}})
    from web.app import app
    cl = app.test_client()
    with cl.session_transaction() as s:
        s["ok"] = True
    h = cl.get("/usage").get_data(as_text=True)
    assert "claude-sonnet-5-5" in h and "$0.03" in h          # 4 × 0.007 = 0.028
    assert "무료" in h and "$4.97" in h                        # 남은 돈 5 - 0.028
    # 하루 $0.028 → 한 달 $0.84 → $5 로 약 6개월
    assert "약 6.0개월" in h, h[h.find("충전하면"):h.find("충전하면") + 1500]
    cl.post("/usage/settings", data={"budget": "10", "pin_claude-sonnet-5-5": "1", "pout_claude-sonnet-5-5": "5"})
    st = pricing.settings(c.data_dir)
    assert st["budget"] == 10 and st["prices"]["claude-sonnet-5-5"] == [1.0, 5.0] and not st["free"]["gemini"]


def test_change_models_from_usage_page():
    import shutil
    import tomllib
    import web.app as W
    tmp = Path(tempfile.mkdtemp()) / "config.toml"
    shutil.copy(Path(__file__).resolve().parent.parent / "config.example.toml", tmp)
    old = W.CONFIG_PATH
    W.CONFIG_PATH = tmp
    try:
        cl = W.app.test_client()
        with cl.session_transaction() as s:
            s["ok"] = True
        h = cl.get("/usage").get_data(as_text=True)
        assert "AI · 모델 바꾸기" in h and "월 ≈" in h
        cl.post("/usage/models", data={"provider": "consensus", "claude_model": "claude-haiku-5-5",
                                       "gemini_model": "gemini-3.8-flash", "openai_model": "gpt-6-luna"})
        a = tomllib.loads(tmp.read_text())["ai"]
        assert (a["provider"], a["claude_model"], a["openai_model"]) == ("consensus", "claude-haiku-5-5", "gpt-6-luna")
        cl.post("/usage/models", data={"provider": "evil", "claude_model": "x"})
        assert tomllib.loads(tmp.read_text())["ai"]["provider"] == "consensus"
    finally:
        W.CONFIG_PATH = old


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
