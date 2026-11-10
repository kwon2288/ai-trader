"""손익 — 국내+미국 통합(원화) 보기."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.update(TRADER_DATA_DIR=tempfile.mkdtemp(), WEB_PASSWORD="secret-pass", TRADING_MODE="dry")

from types import SimpleNamespace  # noqa: E402

from trader import ledger  # noqa: E402
from trader.journal import Journal  # noqa: E402


def _fill(j, mode, side, code, name, qty, px):
    j.add_order(mode, side, code, name, qty, px, SimpleNamespace(ok=True, order_no="X1", message=""), "t", "filled")


def test_combine_converts_us_to_krw():
    from web.views.pnl import combine
    j = Journal(Path(tempfile.mkdtemp()) / "j.db")
    _fill(j, "mock", "BUY", "005930", "삼성전자", 10, 50_000)
    _fill(j, "mock", "SELL", "005930", "삼성전자", 10, 52_000)       # +2만원 (수수료 전)
    _fill(j, "mock_us", "BUY", "KO", "코카콜라", 2, 60.0)
    _fill(j, "mock_us", "SELL", "KO", "코카콜라", 2, 65.0)           # +$10 (수수료 전)
    z = {"kr_fee_pct": 0, "kr_sell_tax_pct": 0, "us_fee_pct": 0}
    r_kr, r_us = ledger.period(j, "mock", None, None, z), ledger.period(j, "mock_us", None, None, z)
    r = combine([(r_kr, 1.0, "KRW"), (r_us, 1400.0, "USD")])
    assert r_kr["pnl"] == 20_000 and r_us["pnl"] == 10.0
    assert r["pnl"] == 34_000 and r["n"] == 2 and r["wins"] == 2 and r["win_rate"] == 100.0
    assert {t["cur"] for t in r["trades"]} == {"KRW", "USD"}
    assert r["by_code"][0]["code"] == "005930" and r["by_code"][1]["pnl"] == 14_000
    assert sum(v for _, v in r["by_day"]) == 34_000


def test_us_day_is_new_york_date():
    """한국 11/11 00:30 매도 = 뉴욕 11/10 장 → 미국 11/10 마감 요약·기간 손익에 들어가야 한다."""
    from datetime import date
    j = Journal(Path(tempfile.mkdtemp()) / "j.db")
    z = {"kr_fee_pct": 0, "kr_sell_tax_pct": 0, "us_fee_pct": 0}
    for ts, side, px in (("2026-11-06T00:30:00+09:00", "BUY", 92.7), ("2026-11-11T00:30:00+09:00", "SELL", 96.19)):
        j.db.execute("INSERT INTO orders(ts,mode,side,code,name,qty,est_price,order_no,ok,message,trigger,status,"
                     "filled_qty,fill_price) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (ts, "mock_us", side, "KO", "코카콜라", 1, px, "X", 1, "", "t", "filled", 1, px))
    j.db.commit()
    r = ledger.period(j, "mock_us", date(2026, 11, 10), date(2026, 11, 10), z)
    assert r["n"] == 1 and abs(r["pnl"] - 3.49) < 0.01
    assert ledger.period(j, "mock_us", date(2026, 11, 11), date(2026, 11, 11), z)["n"] == 0
    assert ledger.period(j, "mock_us", date(2026, 11, 5), date(2026, 11, 5), z)["buys"] == 1   # 한국 11/6 00:30 = 뉴욕 11/5


def test_page_has_all_tab():
    from trader import config as cfgmod
    from web import app as webapp
    c = cfgmod.load()
    j = Journal(c.db_path)
    _fill(j, c.ledger, "BUY", "005930", "삼성전자", 1, 50_000)
    _fill(j, c.ledger, "SELL", "005930", "삼성전자", 1, 51_000)
    j.set("fx_usdkrw", "1400")
    orig = webapp.cfg

    def cfg_us():
        x = orig()
        x.us = {"enabled": True, "total_usd": 200, "symbols": ["KO"]}
        return x
    webapp.cfg = cfg_us
    try:
        cl = webapp.app.test_client()
        with cl.session_transaction() as s:
            s["ok"] = True
        h = cl.get("/pnl?m=all&p=all").get_data(as_text=True)
        assert "🌐 통합" in h and "국내 + 미국 합계" in h and "삼성전자" in h
        h = cl.get("/pnl?p=week").get_data(as_text=True)          # 기간을 바꿔도 통합 유지
        assert "국내 + 미국 합계" in h
        h = cl.get("/pnl?m=kr").get_data(as_text=True)
        assert "국내 + 미국 합계" not in h and "🌐 통합" in h
    finally:
        webapp.cfg = orig


if __name__ == "__main__":
    for k, v in list(globals().items()):
        if k.startswith("test_"):
            v()
    print("ok")
