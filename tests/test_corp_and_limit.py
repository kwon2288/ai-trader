"""액면분할·병합 대비 · 월 AI 사용 한도."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from trader import corpaction, pricing  # noqa: E402
from trader.broker import Holding  # noqa: E402
from trader.journal import Journal  # noqa: E402
import test_bot as tb  # noqa: E402


def H(qty, avg, px):
    return {"NVDA": Holding("NVDA", "엔비디아", qty, avg, px)}


def setup_lot(qty=2, avg=1000.0):
    cfg = tb.make_cfg(Path(tempfile.mkdtemp()))
    cfg = cfg.for_market("kr")
    j = Journal(cfg.db_path)
    j.open_position("NVDA", "엔비디아", cfg.market, qty, avg)
    return cfg, j


def test_split_adjusts_lot():
    cfg, j = setup_lot(2, 1000)
    said = []
    assert corpaction.check(j, cfg, H(2, 1000, 1010), said.append) == set()        # 처음: 기억만
    p = corpaction.check(j, cfg, H(20, 101, 101), said.append)                      # 10:1 분할 반영
    assert p == set()
    assert j.bot_lots(cfg.market, cfg.ledger)["NVDA"] == (20, 100.0)
    assert any("분할 감지" in s for s in said)
    # 병합 1:4 (수량 1/4, 가격 4배)
    corpaction.check(j, cfg, H(5, 404, 404), said.append)
    assert j.bot_lots(cfg.market, cfg.ledger)["NVDA"] == (5, 400.0)


def test_price_moved_before_qty_pauses_then_fixes():
    cfg, j = setup_lot(2, 1000)
    said, sent = [], []
    corpaction.check(j, cfg, H(2, 1000, 1000))
    p = corpaction.check(j, cfg, H(2, 1000, 100), said.append, lambda k, m: sent.append(m))   # 가격만 1/10
    assert p == {"NVDA"} and len(sent) == 1 and "매매를 멈춰요" in sent[0]
    p = corpaction.check(j, cfg, H(2, 1000, 98), said.append, lambda k, m: sent.append(m))    # 여전히 대기 (알림 1번만)
    assert p == {"NVDA"} and len(sent) == 1
    p = corpaction.check(j, cfg, H(20, 100, 99), said.append)                                   # 수량 반영 → 장부 수정, 풀림
    assert p == set() and j.bot_lots(cfg.market, cfg.ledger)["NVDA"] == (20, 100.0)


def test_real_moves_are_not_splits():
    cfg, j = setup_lot(2, 1000)
    corpaction.check(j, cfg, H(2, 1000, 1000))
    assert corpaction.check(j, cfg, H(2, 1000, 700)) == set()                  # -30% 급락은 분할 아님 → 정상 매매(손절)
    assert j.bot_lots(cfg.market, cfg.ledger)["NVDA"] == (2, 1000.0)
    corpaction.check(j, cfg, H(4, 850, 700))                                    # 사용자가 2주 더 삼(가격 그대로) → 분할 아님
    assert j.bot_lots(cfg.market, cfg.ledger)["NVDA"] == (2, 1000.0)


def test_bot_does_not_stoploss_paused():
    cfg, br, _, bot = tb.setup(holdings={"000001": Holding("000001", "종목1", 2, 20_000, 2_000)},
                               prices={"000001": 2_000, "000002": 30_000, "000003": 80_000})
    bot.j.open_position("000001", "종목1", "kr", 2, 20_000)
    bot.j.set("seen_kr_000001", "2|20000|2026-01-01T00:00:00")
    out = bot.run()
    assert not any(o[0] == "SELL" for o in br.orders), out                      # -90% 이지만 분할 대기 → 손절 안 함
    assert "매매를 멈춰요" in out


def test_monthly_ai_limit():
    d = tempfile.mkdtemp()
    os.environ.update(TRADER_DATA_DIR=d, WEB_PASSWORD="secret-pass")
    cfg = tb.make_cfg(Path(d))
    j = Journal(cfg.db_path)
    st = pricing.settings(cfg.data_dir)
    assert pricing.month_status(j, cfg.data_dir)["over"] is False               # 기본 0 = 한도 없음
    st["monthly_limit"] = 0.05
    pricing.save(cfg.data_dir, st)
    j.add_usage(cfg.ledger, "claude", "claude-haiku-5-5", 1_000_000, 0, "timer")  # $0.10
    ms = pricing.month_status(j, cfg.data_dir)
    assert ms["over"] and ms["spend"] == 0.1
    # 실행: AI 대신 규칙만
    from trader import runner
    import trader.analyst as A

    class Boom:
        name, label = "claude", "Claude"

        def decide(self, ctx):
            raise AssertionError("한도 넘었는데 AI 호출")
    orig_mk, orig_br = A.make_analyst, runner.make_broker
    A.make_analyst = lambda c: Boom()
    runner.make_broker = lambda c: tb.FakeBroker({"000001": 20_000, "000002": 30_000, "000003": 80_000})
    try:
        out = runner.run_cycle(cfg, source="timer")
    finally:
        A.make_analyst, runner.make_broker = orig_mk, orig_br
    assert "월 한도" in out and "AI 분석 실패" not in out, out
    import web.app as W
    cl = W.app.test_client()
    with cl.session_transaction() as s:
        s["ok"] = True
    assert "규칙만으로" in cl.get("/usage").get_data(as_text=True)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
