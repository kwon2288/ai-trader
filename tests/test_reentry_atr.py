"""재매수 대기(손실로 판 종목 며칠 안 삼) · ATR 손절 — 규칙·백테스트·실제 봇·웹."""
import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from trader import backtest, indicators, signals, strategy  # noqa: E402
from trader.bot import Bot  # noqa: E402
from trader.journal import Journal  # noqa: E402
import test_bot as tb  # noqa: E402
from test_guard import _bars, _cfg  # noqa: E402

COOL = {"chase_max_ret5": 0, "fail_exit": False, "protect_at": 0, "trail": 0, "reentry_days": 5}


def test_helpers():
    assert strategy.stop_pct(-5, 2, 1.8) == -3.6 and strategy.stop_pct(-5, 2, 0.4) == -2.0     # 하한 2%
    assert strategy.stop_pct(-5, 3, 9) == -15.0 and strategy.stop_pct(-5, 0, 3) == -5.0        # 상한 15% · 끔
    assert strategy.stop_pct(-5, 2, None) == -5.0 and strategy.stop_pct(-5, 2, "x") == -5.0
    assert strategy.busdays_between("2026-11-12", "2026-11-19") == 5                           # 목 → 다음 주 목
    assert strategy.busdays_between("2026-11-13", "2026-11-16") == 1                           # 금 → 월
    s = strategy.settings(SimpleNamespace(market="us", strategy={"stop_atr": "2.5", "us": {"reentry_days": 3}}))
    assert s["stop_atr"] == 2.5 and s["reentry_days"] == 3 and strategy.guard_opts(s)["reentry_days"] == 3
    assert strategy.settings(SimpleNamespace(market="kr", strategy={"stop_atr": "-1"}))["stop_atr"] == 0
    rows = [{"close": 100, "high": 101, "low": 99, "volume": 1}] * 20
    assert abs(indicators.compute(rows)["atr14_pct"] - 2.0) < 1e-9                             # 진폭 2 / 종가 100
    assert indicators.compute(rows[:5])["atr14_pct"] is None


def test_signal_cooldown_skips_buy_only():
    bo = {"code": "A", "name": "가", "held": False, "warnings": [], "affordable": True, "close": 105,
          "high_20d_prev": 100, "volume_vs_20d_avg": 2.0, "rsi14": 65, "ret_5d_pct": 3}
    assert "A" in signals.evaluate([bo], mode="breakout")
    sk = []
    assert signals.evaluate([{**bo, "cooldown": "11-12 손실 매도"}], mode="breakout", skipped=sk) == {}
    assert sk and "재매수 대기" in sk[0][1]


def _crash_and_rebreak():
    flat = [(p, p, p, p, 1000) for p in [10_000, 9_950] * 40]
    return flat + [(10_000, 10_650, 10_000, 10_600, 3000),        # 돌파 → 다음 날 시가 10,500 매수
                   (10_500, 10_550, 10_400, 10_450, 1000),
                   (10_300, 10_300, 9_800, 9_850, 1000),          # −5% 손절선(9,975) 깨짐 → 손절
                   (9_900, 10_000, 9_850, 9_950, 1000),
                   (10_000, 11_000, 10_000, 10_900, 4000),        # 바로 다시 신고가 돌파
                   ] + [(10_900, 10_950, 10_850, 10_900, 1000)] * 10


def test_backtest_reentry_cooldown():
    data = {"000001": _bars(_crash_and_rebreak())}
    kw = dict(stop_pct=-5, take_pct=200, min_hold=30, signal_mode="breakout")
    off = backtest.run(_cfg(), data, {}, guard={**COOL, "reentry_days": 0}, **kw)
    on = backtest.run(_cfg(), data, {}, guard=COOL, **kw)
    assert off["n"] >= 1 and off["open_positions"] == 1                    # 손절 뒤 이틀 만에 다시 삼
    assert on["n"] == 1 and on["trades"][0]["why"] == "손절" and on["open_positions"] == 0


def test_backtest_atr_stop():
    calm = [(p, p + 40, p - 40, p, 1000) for p in [10_000, 9_900] * 40]   # 하루 진폭 ~1.4% → ATR ~1.4~1.7%
    rows = calm + [(10_000, 10_650, 10_000, 10_600, 3000), (10_500, 10_550, 10_450, 10_500, 1000),
                   (10_450, 10_450, 10_050, 10_100, 1000)] + [(10_100, 10_150, 10_050, 10_100, 1000)] * 10
    data = {"000001": _bars(rows)}
    kw = dict(stop_pct=-8, take_pct=200, min_hold=30, signal_mode="breakout")
    fixed = backtest.run(_cfg(), data, {}, **kw)
    atr = backtest.run(_cfg(), data, {}, stop_atr=2, **kw)
    assert fixed["n"] == 0 and fixed["open_positions"] == 1                # −4% 대는 −8% 손절에 안 걸림
    t = atr["trades"][0]
    assert t["why"] == "손절" and -4.0 < t["pnl_pct"] < -2.5 and atr["stop_atr"] == 2.0   # ATR 2배 ≈ −3.3% 에서 정리
    assert backtest._atr_mult("atr2") == 2.0 and backtest._atr_mult(-5) == 0


def test_short_sweep_has_atr_rows():
    data = {"000001": _bars(_crash_and_rebreak())}
    from unittest import mock
    with mock.patch.object(backtest, "prepare", return_value=(data, {}, None, None)):
        cfg = _cfg()
        cfg.market = "kr"
        out = backtest.sweep(cfg, None, 250, ["000001"], "069500", kind="short",
                             grid={"signal": ["breakout"], "take": [10], "max_hold": [0], "market_filter": [False]})
    atr_rows = [x for x in out["rows"] if x["stop_atr"]]
    assert len(out["rows"]) == 8 and len(atr_rows) == 2 and all(x["stop"] == cfg.stop_loss_pct for x in atr_rows)


def _bot():
    cfg = tb.make_cfg(Path(tempfile.mkdtemp()))
    b = Bot.__new__(Bot)
    b.cfg, b.j, b.lines = cfg, Journal(cfg.db_path), []
    b.say = b.lines.append
    return b


def test_bot_cooldown_from_loss_sells():
    b = _bot()
    ok = SimpleNamespace(ok=True, order_no="1", message="")
    L = b.cfg.ledger
    b.j.add_order(L, "BUY", "000001", "손실", 2, 10_000, ok, "t", "filled")
    b.j.add_order(L, "SELL", "000001", "손실", 2, 9_000, ok, "손절", "filled")
    b.j.add_order(L, "BUY", "000002", "이익", 2, 10_000, ok, "t", "filled")
    b.j.add_order(L, "SELL", "000002", "이익", 2, 11_000, ok, "익절", "filled")
    cd = b._cooldown(5)
    assert set(cd) == {"000001"} and "손실 매도" in cd["000001"] and "5거래일" in cd["000001"]
    assert b._cooldown(0) == {}


def test_bot_atr_stop_per_position():
    b = _bot()
    b.cfg.stop_loss_pct = -8
    b.j.open_position("000001", "종목1", "kr", 2, 10_000)
    h = SimpleNamespace(name="종목1", avg_price=10_000, now_price=9_700, pnl_pct=-3.0)
    assert b._exit_trigger(h, "000001") is None                                  # 고정 −8%
    b.cfg.strategy = {"stop_atr": 2, "guard": False}
    b._set_atr_stops([{"code": "000001", "atr14_pct": 1.2}], ["000001"], 2)
    assert b._stop_for("000001") == -2.4 and "ATR 손절선" in b.lines[-1]
    assert b._exit_trigger(h, "000001") == "손절"
    b._set_atr_stops([{"code": "000001", "atr14_pct": 5}], ["000001"], 2)       # 한 번 정하면 그대로
    assert b._stop_for("000001") == -2.4
    b.cfg.strategy = {"guard": False}                                            # ATR 끄면 고정 % 로
    assert b._stop_for("000001") == -8 and b._exit_trigger(h, "000001") is None


def test_web_save_and_apply():
    import tomllib
    os.environ.update(TRADER_DATA_DIR=tempfile.mkdtemp(), WEB_PASSWORD="secret-pass")
    import web.app as W
    p = Path(tempfile.mkdtemp()) / "config.toml"
    shutil.copy(Path(__file__).resolve().parent.parent / "config.example.toml", p)
    old = W.CONFIG_PATH
    W.CONFIG_PATH = p
    try:
        cl = W.app.test_client()
        with cl.session_transaction() as s:
            s["ok"] = True
        h = cl.get("/settings").get_data(as_text=True)
        assert "kr_stop_atr" in h and "reentry_days" in h
        form = {"kr_signal": "dip", "kr_max_hold_days": "0", "kr_stop": "-5", "kr_take": "10", "kr_min_hold": "1",
                "kr_core_pct": "0", "kr_guard": "on", "kr_stop_atr": "2.5", "reentry_days": "3"}
        cl.post("/strategy/save", data=form)
        t = tomllib.loads(p.read_text())["strategy"]
        assert t["kr"]["stop_atr"] == 2.5 and t["reentry_days"] == 3
        cl.post("/strategy/save", data={**form, "kr_stop_atr": "99"})                     # 범위 밖 → 거부
        assert tomllib.loads(p.read_text())["strategy"]["kr"]["stop_atr"] == 2.5
        base = {"stop": "-5", "take": "7", "min_hold": "1", "core_pct": "0", "market_filter": "0",
                "signal": "dip", "max_hold": "0", "market": "us"}
        cl.post("/strategy/apply", data={**base, "stop_atr": "2"})
        assert tomllib.loads(p.read_text())["strategy"]["us"]["stop_atr"] == 2
        cl.post("/strategy/apply", data=base)                                           # 고정 손절 줄 → ATR 끔
        assert tomllib.loads(p.read_text())["strategy"]["us"]["stop_atr"] == 0
        assert "ATR 손절 배수" in cl.get("/backtest").get_data(as_text=True)
    finally:
        W.CONFIG_PATH = old


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
