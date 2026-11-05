"""증권사를 바꿨다가 돌아와도 봇이 산 종목을 잊지 않는다."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from trader import ledger  # noqa: E402
from trader.bot import Bot  # noqa: E402
from trader.broker import Holding  # noqa: E402
from trader.journal import Journal  # noqa: E402
import test_bot as tb  # noqa: E402


def test_switch_away_and_back():
    os.environ.pop("BROKER", None)
    cfg, nh, ai, bot = tb.setup(holdings={"000001": Holding("000001", "종목1", 3, 20_000, 20_000)})
    j = bot.j
    assert j.broker == "nh/mock"
    j.open_position("000001", "종목1", "kr", 1, 20_000)                    # 봇 1주 + 사용자 2주 (나무)
    # 토스로 변경: 토스 계좌엔 그 종목이 없음
    os.environ["BROKER"] = "toss"
    try:
        toss = tb.FakeBroker({"000001": 20_000, "000002": 30_000, "000003": 80_000}, holdings={})
        b2 = Bot(cfg, toss, ai, Journal(cfg.db_path))
        assert b2.j.broker == "toss/mock"
        out = b2.run()
        assert "다른 증권사에 남겨 둔 봇 종목" in out and "종목1 1주(나무 모의투자)" in out
        assert b2.j.bot_codes("kr") == {}                                    # 토스 장부엔 없음
        assert Journal(cfg.db_path).bot_lots("kr") == {"000001": (1, 20_000.0)}   # 지워지지 않음
        st = ledger.status(b2.j, cfg, {}, 1_000_000)
        assert st["room"] == cfg.total_krw - 20_000 and st["parked"][0]["broker"] == "나무 모의투자"   # 예산에서 빼 둠
        assert not any(o[1] == "000001" for o in toss.orders)               # 남겨 둔 종목은 토스에서 안 삼
    finally:
        os.environ.pop("BROKER", None)
    # 나무로 돌아옴 → 다시 봇 1주로 관리
    b3 = Bot(cfg, nh, ai, Journal(cfg.db_path))
    assert b3.j.bot_codes("kr").keys() == {"000001"}
    from trader.bot import own_holdings
    held = own_holdings(b3.j, cfg, nh.holdings)
    assert held["000001"].qty == 1


def test_mock_and_live_are_separate():
    """모의에서 봇이 산 종목을, 실계좌로 바꿨을 때 실계좌의 사용자 주식으로 착각하지 않는다."""
    os.environ.pop("BROKER", None)
    cfg, br, ai, bot = tb.setup(holdings={"000001": Holding("000001", "종목1", 2, 20_000, 20_000)})
    bot.j.open_position("000001", "종목1", "kr", 2, 20_000)                 # 모의: 봇 2주
    from dataclasses import replace
    live = replace(cfg, mode="live")
    user = tb.FakeBroker({"000001": 15_000, "000002": 30_000, "000003": 80_000},
                         holdings={"000001": Holding("000001", "종목1", 5, 30_000, 15_000)})   # 실계좌: 사용자 5주 (-50%)
    b2 = Bot(live, user, ai, Journal(cfg.db_path))
    from trader.bot import own_holdings
    assert own_holdings(b2.j, live, user.holdings) == {}                      # 봇 몫 없음
    b2.run()
    assert not any(o[0] == "SELL" for o in user.orders)                       # 사용자 주식 손절 안 함
    assert Journal(cfg.db_path).bot_lots("kr") == {"000001": (2, 20_000.0)}  # 모의 기록은 그대로
    # v0.22.0 처럼 증권사만 적힌 기록은 지금 모드 것으로
    j = Journal(cfg.db_path)
    j.db.execute("UPDATE bot_positions SET broker='nh'")
    j.use_broker("nh", "kr", "mock")
    assert j.bot_codes("kr").keys() == {"000001"}


def test_us_session_day_and_unknown_buy():
    from trader import fills
    # 한국 23:40 주문과 다음 날 04:00 확인은 같은 뉴욕 거래일
    assert fills.session_day("2026-11-03T23:40:00+09:00", "us") == "2026-11-03"
    assert fills.session_day("2026-11-04T04:00:00+09:00", "us") == "2026-11-03"
    assert fills.session_day("2026-11-04T09:10:00+09:00", "kr") == "2026-11-04"
    cfg, br, ai, bot = tb.setup()
    j = bot.j
    from trader.broker import OrderResult
    rid = j.add_order(cfg.ledger, "BUY", "000002", "종목2", 1, 30_000, OrderResult(True, "1", "ok"), "AI", status="sent")
    j.db.execute("UPDATE orders SET ts='2026-01-02T10:00:00+09:00' WHERE rowid=?", (rid,))
    j.db.commit()

    class B:
        def order_status(self, *a):
            return None

        def cancel(self, *a):
            raise AssertionError
    said = []
    fills.reconcile(j, cfg, B(), said.append)
    row = j.db.execute("SELECT status FROM orders WHERE rowid=?", (rid,)).fetchone()
    assert row["status"] == "unknown" and "000002" not in j.bot_codes("kr")   # 못 찾은 매수는 산 걸로 안 봄
    assert any("찾지 못했어요" in s for s in said)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
