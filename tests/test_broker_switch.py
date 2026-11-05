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
    assert j.broker == "nh"
    j.open_position("000001", "종목1", "kr", 1, 20_000)                    # 봇 1주 + 사용자 2주 (나무)
    # 토스로 변경: 토스 계좌엔 그 종목이 없음
    os.environ["BROKER"] = "toss"
    try:
        toss = tb.FakeBroker({"000001": 20_000, "000002": 30_000, "000003": 80_000}, holdings={})
        b2 = Bot(cfg, toss, ai, Journal(cfg.db_path))
        assert b2.j.broker == "toss"
        out = b2.run()
        assert "다른 증권사에 남겨 둔 봇 종목" in out and "종목1 1주(nh)" in out
        assert b2.j.bot_codes("kr") == {}                                    # 토스 장부엔 없음
        assert Journal(cfg.db_path).bot_lots("kr") == {"000001": (1, 20_000.0)}   # 지워지지 않음
        st = ledger.status(b2.j, cfg, {}, 1_000_000)
        assert st["room"] == cfg.total_krw - 20_000 and st["parked"][0]["broker"] == "nh"   # 예산에서 빼 둠
        assert not any(o[1] == "000001" for o in toss.orders)               # 남겨 둔 종목은 토스에서 안 삼
    finally:
        os.environ.pop("BROKER", None)
    # 나무로 돌아옴 → 다시 봇 1주로 관리
    b3 = Bot(cfg, nh, ai, Journal(cfg.db_path))
    assert b3.j.bot_codes("kr").keys() == {"000001"}
    from trader.bot import own_holdings
    held = own_holdings(b3.j, cfg, nh.holdings)
    assert held["000001"].qty == 1


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
