"""시장별 전략 · 규칙 매도 바로 실행."""
import os
import shutil
import sys
import tempfile
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from trader import config as cfgmod, strategy  # noqa: E402
from trader.broker import Holding  # noqa: E402
import test_bot as tb  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def cfg_file(extra: str) -> Path:
    p = Path(tempfile.mkdtemp()) / "config.toml"
    p.write_text((ROOT / "config.example.toml").read_text() + "\n" + extra)
    return p


def test_per_market_config():
    os.environ["TRADER_DATA_DIR"] = tempfile.mkdtemp()
    p = cfg_file('[strategy.kr]\nsignal = "breakout"\nstop_loss_pct = -5.0\ntake_profit_pct = 10.0\n'
                 '[strategy.us]\nsignal = "dip"\ntake_profit_pct = 7.0\nmax_hold_days = 10\nmarket_filter = true\n')
    raw = tomllib.loads(p.read_text())
    c = cfgmod.load(p)
    us = c.for_market("us")
    assert strategy.settings(c)["signal"] == "breakout" and strategy.settings(us)["signal"] == "dip"
    assert c.stop_loss_pct == -5.0 and c.take_profit_pct == 10.0
    assert us.take_profit_pct == 7.0 and us.stop_loss_pct == float(raw["risk"]["stop_loss_pct"])   # 미국 손절은 공통값
    assert strategy.settings(us)["max_hold_days"] == 10 and strategy.settings(us)["market_filter"] is True
    assert us.for_market("kr").stop_loss_pct == -5.0
    assert strategy.settings(c)["rule_sell"] == "direct"


def test_save_and_apply_routes():
    os.environ.update(TRADER_DATA_DIR=tempfile.mkdtemp(), WEB_PASSWORD="secret-pass")
    import web.app as W
    p = Path(tempfile.mkdtemp()) / "config.toml"
    shutil.copy(ROOT / "config.example.toml", p)
    old = W.CONFIG_PATH
    W.CONFIG_PATH = p
    try:
        cl = W.app.test_client()
        with cl.session_transaction() as s:
            s["ok"] = True
        form = {"core_pct": "0", "rule_sell": "direct"}
        for mk, sig, stop in (("kr", "breakout", "-5"), ("us", "dip", "-6")):
            form.update({f"{mk}_signal": sig, f"{mk}_max_hold_days": "10", f"{mk}_stop": stop, f"{mk}_take": "8",
                         f"{mk}_min_hold": "1"})
        form["us_market_filter"] = "on"
        form["kr_core_pct"] = "70"
        form["us_core_pct"] = "0"
        cl.post("/strategy/save", data=form)
        t = tomllib.loads(p.read_text())
        assert t["strategy"]["kr"]["signal"] == "breakout" and t["strategy"]["us"]["stop_loss_pct"] == -6
        assert t["strategy"]["us"]["market_filter"] is True and t["strategy"]["kr"]["market_filter"] is False
        assert t["strategy"]["rule_sell"] == "direct"
        assert t["strategy"]["kr"]["core_pct"] == 70 and t["strategy"]["us"]["core_pct"] == 0     # 코어도 시장별
        c = cfgmod.load(p)
        assert strategy.settings(c)["core_pct"] == 70 and strategy.settings(c.for_market("us"))["core_pct"] == 0
        assert strategy.settings(c)["core_code"] == "069500"
        # 백테스트 ‘적용’ — 그 시장에만
        cl.post("/strategy/apply", data={"market": "us", "stop": "-3", "take": "5", "min_hold": "1", "core_pct": "0",
                                         "signal": "volume", "max_hold": "5", "market_filter": "0"})
        t = tomllib.loads(p.read_text())
        assert t["strategy"]["us"]["signal"] == "volume" and t["strategy"]["us"]["stop_loss_pct"] == -3
        assert t["strategy"]["kr"]["signal"] == "breakout"
        h = cl.get("/settings").get_data(as_text=True)
        assert "🇺🇸 미국" in h and "바로 매도" in h
    finally:
        W.CONFIG_PATH = old


def test_rule_sell_direct_skips_ai():
    cfg, br, ai, bot = tb.setup(holdings={"000001": Holding("000001", "종목1", 2, 20_000, 21_000)},
                                decisions=[{"code": "000001", "action": "HOLD", "confidence": 0.9, "reason": "유지"}])
    cfg.ai_hybrid = True
    cfg.strategy = {"signal": "dip"}
    bot.j.open_position("000001", "종목1", "kr", 2, 20_000)
    bot.j.db.execute("UPDATE bot_positions SET opened='2026-01-01'")
    import trader.indicators as ind
    orig = ind.compute
    ind.compute = lambda bars: {**orig(bars), "rsi14": 65}                  # 반등 완료 → 규칙 매도
    try:
        out = bot.run()
    finally:
        ind.compute = orig
    assert ("SELL", "000001", 2) in br.orders, out
    assert "AI 확인 없이" in out and ai.ctx is None                        # 매수 신호 없으면 AI 안 부름
    # ai 모드면 AI 가 '유지'로 막음
    cfg2, br2, ai2, bot2 = tb.setup(holdings={"000001": Holding("000001", "종목1", 2, 20_000, 21_000)},
                                    decisions=[{"code": "000001", "action": "HOLD", "confidence": 0.9, "reason": "유지"}])
    cfg2.ai_hybrid = True
    cfg2.strategy = {"signal": "dip", "rule_sell": "ai"}
    bot2.j.open_position("000001", "종목1", "kr", 2, 20_000)
    bot2.j.db.execute("UPDATE bot_positions SET opened='2026-01-01'")
    ind.compute = lambda bars: {**orig(bars), "rsi14": 65}
    try:
        bot2.run()
    finally:
        ind.compute = orig
    assert not any(o[0] == "SELL" for o in br2.orders) and ai2.ctx is not None


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
