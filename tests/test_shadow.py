"""규칙만 그림자 장부 — AI 없이 규칙 신호만 따랐다면 (주문 없음)."""
import os
import sys
import tempfile
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from trader import shadow  # noqa: E402
from trader.journal import Journal, now  # noqa: E402
import test_bot as tb  # noqa: E402


def _cfg(**kw):
    cfg = tb.make_cfg(Path(tempfile.mkdtemp()), **kw)
    cfg.strategy = {"signal": "breakout", "late_buy": False, "guard": True, "chase_max_ret5": 12, "reentry_days": 5}
    cfg.costs = {"kr_fee_pct": 0, "kr_sell_tax_pct": 0}
    return cfg


def _bo(code, price, **kw):
    """신고가 돌파 매수 신호가 나는 후보."""
    return {"code": code, "name": f"종목{code[-1]}", "held": False, "warnings": [], "affordable": True, "price": price,
            "close": price, "high_20d_prev": price * 0.97, "volume_vs_20d_avg": 2.0, "rsi14": 65, "ret_5d_pct": 4,
            "atr14_pct": 2.0, **kw}


def _flat(code, price):
    return {"code": code, "name": f"종목{code[-1]}", "held": False, "warnings": [], "affordable": True, "price": price,
            "close": price, "high_20d_prev": price * 1.2, "volume_vs_20d_avg": 1.0, "rsi14": 50, "ret_5d_pct": 0}


def test_buys_on_rule_signal_with_limits_and_no_orders():
    cfg = _cfg()
    j = Journal(cfg.db_path)
    lines = []
    cands = [_bo("000001", 10_000), _bo("000002", 20_000), _bo("000003", 5_000), _flat("000004", 1_000)]
    r = shadow.step(j, cfg, cands, {c["code"]: c["price"] for c in cands}, None, lines.append)
    pos = shadow.positions(j, cfg.ledger)
    assert r["bought"] == 2 and len(pos) == 2                                   # 최대 2종목
    assert all(p["cost"] <= 50_000 for p in pos.values())                       # 종목당 5만원
    assert "000004" not in pos and any("그림자(규칙만) 매수" in x for x in lines)
    assert j.db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0        # 실제 주문 없음
    assert shadow.start_day(j, cfg.ledger) == now().date().isoformat()
    j2 = Journal(_cfg().db_path)                                                 # 시장 필터 등으로 막히면 안 삼
    assert shadow.step(j2, cfg, cands, {c["code"]: c["price"] for c in cands}, "시장 필터", lines.append)["bought"] == 0


def test_chase_block_and_stop_and_cooldown():
    cfg = _cfg()
    j = Journal(cfg.db_path)
    hot = _bo("000001", 10_000, ret_5d_pct=20)                                  # 5일 +20% → 추격 차단
    assert shadow.step(j, cfg, [hot], {"000001": 10_000}, None, print)["bought"] == 0
    c = _bo("000002", 10_000)
    shadow.step(j, cfg, [c], {"000002": 10_000}, None, print)
    assert shadow.positions(j, cfg.ledger)["000002"]["stop"] == -7               # ATR 끔 → 고정 손절
    r = shadow.step(j, cfg, [dict(c, price=9_200, close=9_200)], {"000002": 9_200}, None, print)
    assert r["sold"] == 1 and r["bought"] == 0                                   # 손절 뒤 같은 날 재매수 안 함(대기)
    t = dict(j.db.execute("SELECT * FROM shadow_trades").fetchone())
    assert t["reason"] == "손절" and t["pnl"] < 0
    s = shadow.summary(j, cfg)
    assert s["n"] == 1 and s["open"] == 0 and s["pct"] < 0


def test_atr_stop_take_protect_and_watch():
    cfg = _cfg()
    cfg.strategy["stop_atr"] = 2
    j = Journal(cfg.db_path)
    shadow.step(j, cfg, [_bo("000001", 10_000)], {"000001": 10_000}, None, print)
    assert shadow.positions(j, cfg.ledger)["000001"]["stop"] == -4.0              # ATR 2% × 2
    assert shadow.watch(j, cfg, {"000001": 10_600}, print) == 0                  # +6% → 이익 보호선 생김
    assert shadow.watch(j, cfg, {"000001": 10_040}, print) == 1                  # 고점 대비 -5.3% → 이익 보호(장중)
    assert j.db.execute("SELECT reason FROM shadow_trades").fetchone()[0] == "이익 보호(장중)"


def test_compare_with_bot_and_verdict():
    cfg = _cfg()
    j = Journal(cfg.db_path)
    shadow.step(j, cfg, [_bo("000001", 10_000)], {"000001": 10_000}, None, print)
    assert shadow.compare(j, cfg)["verdict"].startswith("아직")                  # 봇 자산 기록 없음
    d0 = (now() - timedelta(days=1)).isoformat(timespec="seconds")
    j.db.execute("INSERT INTO runs(ts,mode,equity) VALUES(?,?,?)", (d0, cfg.ledger, 100_000))
    j.db.execute("INSERT INTO runs(ts,mode,equity) VALUES(?,?,?)", (now().isoformat(timespec="seconds"), cfg.ledger, 103_000))
    j.db.commit()
    shadow.watch(j, cfg, {"000001": 10_100}, print)                              # 그림자 평가 +1%·5만원 → +0.5%
    c = shadow.compare(j, cfg)
    assert c["bot_pct"] == 3.0 and c["pct"] == 0.5 and c["gap"] == 2.5 and "10건" in c["verdict"]
    assert shadow.line(j, cfg).startswith("AI vs 규칙만")


def test_bot_run_feeds_shadow_when_ai_rejects():
    """하이브리드: 규칙 신호를 AI 가 막아도 그림자는 산다."""
    from trader.bot import Bot
    cfg = _cfg(ai_hybrid=True)
    cfg.mode = "dry"
    br = tb.FakeBroker({"000001": 10_000, "000002": 10_000, "000003": 10_000})
    import trader.indicators as ind
    orig = ind.compute
    ind.compute = lambda bars: {k: v for k, v in _bo("x", 10_000).items()
                                if k not in ("code", "name", "held", "warnings", "affordable", "price")}
    try:
        ai = tb.FakeAI([])                                                       # AI: 아무것도 안 삼
        b = Bot(cfg, br, ai, Journal(cfg.db_path))
        b.run(force=True)
    finally:
        ind.compute = orig
    assert not [o for o in br.orders if o[0] == "BUY"]
    assert len(shadow.positions(b.j, cfg.ledger)) == 2


def test_pnl_page_card():
    os.environ.update(TRADER_DATA_DIR=tempfile.mkdtemp(), WEB_PASSWORD="secret-pass", TRADING_MODE="dry")
    from web import app as W
    c = W.cfg()
    j = Journal(c.db_path)
    c.strategy = {"signal": "breakout", "late_buy": False}
    shadow.step(j, c, [_bo("005930", 10_000)], {"005930": 10_000}, None, print)
    cl = W.app.test_client()
    with cl.session_transaction() as s:
        s["ok"] = True
    h = cl.get("/pnl?m=kr&p=all").get_data(as_text=True)
    if c.ai_provider != "rules":
        assert "AI vs 규칙만" in h and "그림자" in h


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
