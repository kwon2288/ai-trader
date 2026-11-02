"""국내·미국 같은 계좌(증거금 공유) — 주문가능 현금을 남은 예산 비율로 나눔 · 자동 실행 건너뜀 기록."""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from trader import ledger  # noqa: E402
from trader.journal import Journal  # noqa: E402
import test_bot as tb  # noqa: E402


def cfgs(shared="auto"):
    kr = tb.make_cfg(Path(tempfile.mkdtemp()), total_krw=200_000)
    kr.us = {"enabled": True, "total_usd": 200, "margin_currency": "krw", "shared_cash": shared}
    return kr, kr.for_market("us")


def test_detect():
    for k in ("BROKER", "BROKER_US", "NHPLUG_ACCOUNT", "NHPLUG_ACCOUNT_US"):
        os.environ.pop(k, None)
    kr, us = cfgs()
    assert ledger.shared_account(kr) and ledger.shared_account(us)
    assert us.for_market("kr").market == "kr" and us.for_market("kr").total_krw == 200_000
    os.environ.update(NHPLUG_ACCOUNT="111", NHPLUG_ACCOUNT_US="222")
    assert not ledger.shared_account(kr)                      # 계좌가 다르면 공유 아님
    os.environ.pop("NHPLUG_ACCOUNT_US")
    kr.us["margin_currency"] = "usd"
    assert not ledger.shared_account(kr)                      # 달러 예수금으로 사면 공유 아님
    kr.us["shared_cash"] = "false"
    kr.us["margin_currency"] = "krw"
    assert not ledger.shared_account(kr)
    os.environ.pop("NHPLUG_ACCOUNT")


def test_split_when_cash_short():
    kr, us = cfgs()
    j = Journal(kr.db_path)
    # 환율 모름 → 나누지 않음
    st = ledger.status(j, kr, {}, 150_000)
    assert st["avail"] == 150_000 and st["shared"]["fx"] is None
    j.set("fx_usdkrw", "1400")
    # 국내 남은 예산 20만원, 미국 남은 예산 $200 = 28만원 → 합 48만원 > 현금 24만원 → 국내 41.7%
    st = ledger.status(j, kr, {}, 240_000)
    assert st["shared"]["short"] and st["shared"]["pct"] == 42 and st["avail"] == 100_000
    st_us = ledger.status(j, us, {}, 240_000 / 1400)
    assert abs(st_us["avail"] - 100) < 0.5                     # 미국도 같은 비율 → $100 (= 14만원)
    # 현금이 넉넉하면 나누지 않음
    st = ledger.status(j, kr, {}, 1_000_000)
    assert st["avail"] == 200_000 and not st["shared"]["short"]
    # 미국이 이미 $150 투자 중이면 미국 남은 예산 $50(7만원)만 남겨 둠
    j.open_position("SPY", "SPY", "us", 1, 150)
    st = ledger.status(j, kr, {}, 240_000)
    assert st["avail"] == round(240_000 * 200_000 / 270_000)


def test_priority_options():
    kr, us = cfgs()
    j = Journal(kr.db_path)
    j.set("fx_usdkrw", "1400")
    # 국내 남은 20만원, 미국 남은 28만원, 현금 24만원
    kr.us["shared_priority"] = "kr"
    assert ledger.status(j, kr, {}, 240_000)["avail"] == 200_000               # 국내 우선: 국내 예산 다 채움
    assert abs(ledger.status(j, kr.for_market("us"), {}, 240_000 / 1400)["avail"] - 40_000 / 1400) < 0.05   # 미국은 남는 4만원
    kr.us["shared_priority"] = "us"
    assert ledger.status(j, kr, {}, 240_000)["avail"] == 0                      # 미국 우선: 미국 28만원 > 현금 → 국내 0
    assert abs(ledger.status(j, kr.for_market("us"), {}, 240_000 / 1400)["avail"] - 240_000 / 1400) < 0.05
    kr.us["shared_priority"] = "weird"
    assert ledger.priority(kr) == "split"


def test_bot_says_split():
    for k in ("BROKER", "BROKER_US", "NHPLUG_ACCOUNT", "NHPLUG_ACCOUNT_US"):
        os.environ.pop(k, None)
    cfg, br, ai, bot = tb.setup(decisions=[])
    cfg.us = {"enabled": True, "total_usd": 200, "margin_currency": "krw"}
    bot.j.set("fx_usdkrw", "1400")
    from trader.broker import Balance
    br.balance = lambda: Balance(50_000, dict(br.holdings))
    out = bot.run()
    assert "같은 계좌" in out, out


def test_skips_are_logged():
    tmp = tempfile.mkdtemp()
    env = {**os.environ, "TRADER_DATA_DIR": tmp, "INVOCATION_ID": "x", "I_UNDERSTAND_REAL_MONEY": ""}
    import json
    Path(tmp, "mode.json").write_text(json.dumps({"mode": "live"}))     # 웹에서 확인 없이 실계좌 → 허가 없음
    root = Path(__file__).resolve().parent.parent
    r = subprocess.run([sys.executable, "run.py", "trade"], cwd=root, env=env, capture_output=True, text=True)
    assert r.returncode != 0
    logs = Journal(Path(tmp) / "journal.db").logs(5)
    assert logs and "실행 허가가 없어 건너뜀" in logs[0]["output"] and logs[0]["source"] == "timer"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
