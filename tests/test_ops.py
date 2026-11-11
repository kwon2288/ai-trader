"""운영 보강 — 종목명 정리, 디스크 경고, 통합 마감 요약, 토스 달러 부족 안내."""
import os
import sys
import tempfile
from collections import namedtuple
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.update(NHPLUG_BASE_URL="https://moapi.nhplug.com:8443", NHPLUG_ACCOUNT="50001234567")

import nhplug  # noqa: E402,F401

from test_bot import make_cfg  # noqa: E402
from trader import monitor, runner  # noqa: E402
from trader.broker import clean_name  # noqa: E402
from trader.journal import KST, Journal  # noqa: E402


def _fill(j, mode, side, code, name, qty, px, ts):
    j.db.execute("INSERT INTO orders(ts,mode,side,code,name,qty,est_price,order_no,ok,message,trigger,status,"
                 "filled_qty,fill_price,fx) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (ts, mode, side, code, name, qty, px, "X", 1, "", "t", "filled", qty, px,
                  1400 if mode.endswith("_us") else None))
    j.db.commit()


def test_clean_name():
    assert clean_name("*HMM") == "HMM" and clean_name(" *LG유플러스 ") == "LG유플러스"
    assert clean_name("삼성전자") == "삼성전자" and clean_name("", "005930") == "005930" and clean_name(None, "KO") == "KO"


def test_old_names_cleaned_once():
    p = Path(tempfile.mkdtemp()) / "j.db"
    j = Journal(p)
    _fill(j, "mock", "BUY", "011200", "*HMM", 1, 100, "2026-10-30T14:45:00+09:00")
    j.db.execute("DELETE FROM state WHERE key='names_cleaned'")
    j.db.commit()
    j.close()
    j = Journal(p)
    assert j.db.execute("SELECT name FROM orders").fetchone()["name"] == "HMM" and j.get("names_cleaned") == "1"


def test_disk_check_warns_once_a_day():
    cfg = make_cfg(Path(tempfile.mkdtemp()))
    j = Journal(cfg.db_path)
    U = namedtuple("U", "total used free")
    sent = []
    send = lambda c, kind, msg: sent.append((kind, msg))            # noqa: E731
    now = datetime(2026, 11, 11, 10, 0, tzinfo=KST)
    assert monitor.disk_check(j, cfg, send, now, usage=lambda p: U(100, 50, 50)) is None and not sent
    m = monitor.disk_check(j, cfg, send, now, usage=lambda p: U(8_000_000_000, 7_600_000_000, 400_000_000))
    assert m and "95%" in m and sent[0][0] == "errors" and "0.4GB" in m
    assert monitor.disk_check(j, cfg, send, now, usage=lambda p: U(100, 99, 1)) is None and len(sent) == 1
    assert monitor.disk_check(j, cfg, send, now.replace(day=12), usage=lambda p: U(100, 99, 1)) and len(sent) == 2


def test_combined_summary():
    cfg = make_cfg(Path(tempfile.mkdtemp()))
    cfg.us = {"enabled": True, "total_usd": 200, "symbols": ["KO"]}
    cfg.fee_pct = cfg.sell_tax_pct = 0
    j = Journal(cfg.db_path)
    j.set("fx_usdkrw", "1400")
    kr, us = cfg.ledger, cfg.for_market("us").ledger
    _fill(j, kr, "BUY", "005930", "삼성전자", 1, 50_000, "2026-11-10T10:01:00+09:00")
    _fill(j, kr, "SELL", "005930", "삼성전자", 1, 51_000, "2026-11-10T14:30:00+09:00")
    _fill(j, us, "BUY", "KO", "코카콜라", 1, 90.0, "2026-11-06T00:30:00+09:00")
    _fill(j, us, "SELL", "KO", "코카콜라", 1, 95.0, "2026-11-11T00:30:00+09:00")     # 뉴욕 11/10
    t = runner.combined_summary(cfg, date(2026, 11, 10))
    assert "통합 마감 요약 (11/10)" in t and "매수 1건 · 매도 2건" in t, t
    line = [x for x in t.splitlines() if "실현 손익" in x][0]
    assert "국내" in line and "미국 $" in line and "≈" in line, line


def test_toss_usd_short_hint():
    from trader.bot import Bot
    cfg = make_cfg(Path(tempfile.mkdtemp()))
    cfg.us = {"enabled": True, "total_usd": 300, "symbols": ["KO"]}
    u = cfg.for_market("us")
    u.mode = "live"
    b = Bot.__new__(Bot)
    b.cfg, b.j, b.lines, sent = u, Journal(u.db_path), [], []
    b.say = b.lines.append
    b.notify = lambda kind, text: sent.append((kind, text))
    b.broker = SimpleNamespace(krw_cash=lambda: 500_000)
    q = {"KO": SimpleNamespace(price=96.0)}
    b._usd_short_hint({"account_cash": 0.0, "room": 300.0}, q)
    assert "자동환전이 안 돼요" in b.lines[0] and "원화 500,000원" in b.lines[0]
    assert sent and sent[0][0] == "errors" and "환전" in sent[0][1]
    b._usd_short_hint({"account_cash": 0.0, "room": 300.0}, q)
    assert len(sent) == 1 and len(b.lines) == 2                    # 알림은 하루 한 번, 로그는 매번
    b.lines.clear()
    b._usd_short_hint({"account_cash": 120.0, "room": 300.0}, q)   # 한 주는 살 수 있음 → 로그만
    assert len(b.lines) == 1 and len(sent) == 1
    b.lines.clear()
    b._usd_short_hint({"account_cash": 400.0, "room": 300.0}, q)   # 충분
    b.broker = SimpleNamespace()                                    # NH 등 (krw_cash 없음)
    b._usd_short_hint({"account_cash": 0.0, "room": 300.0}, q)
    assert not b.lines


if __name__ == "__main__":
    for k, v in list(globals().items()):
        if k.startswith("test_"):
            v()
    print("ok")
