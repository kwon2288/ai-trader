"""자동 종목 선정 오프라인 테스트 — 가짜 종목마스터·가짜 시세로 필터·순위·하루 1회 검색을 검증한다."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_bot import FakeAI, FakeBroker, make_cfg  # noqa: E402
from trader import universe  # noqa: E402
from trader.bot import Bot  # noqa: E402
from trader.journal import Journal  # noqa: E402


def row(code, name, price, cap, market="1", venture="0", **flags):
    r = {"sCode": code, "sMarket": market, "name": name, "sKorName": " " + name, "gVenture": venture,
         "sPrePrice": f"{price:07d}", "prdy_avls": f"{cap:012d}", "eUnder": "N", "eStop": "N", "eWarn": "N",
         "eGongsi": "N", "invt_epmd_issu_yn": "N", "sltr_yn": "N", "short_over_issu_cls_code": "0", "alert_gb": "0"}
    r.update(flags)
    return r


MASTER = [
    row("100010", "좋은회사", 20000, 9000),
    row("100020", "작은회사", 20000, 500),                       # 시총 미달
    row("100030", "비싼회사", 90000, 50000),                     # 가격 초과
    row("100040", "관리종목", 20000, 9000, eUnder="Y"),
    row("100050", "단기과열", 20000, 9000, short_over_issu_cls_code="2"),
    row("100060", "경고종목", 20000, 9000, alert_gb="2"),
    row("100075", "좋은회사우", 15000, 9000),                    # 우선주
    row("100080", "하나스팩1호", 2000, 9000),                    # 스팩
    row("100090", "코스닥회사", 30000, 6000, market="4", venture="A"),
    row("100100", "KODEX 200", 40000, 80000, venture="8"),       # ETF
    row("100110", "KODEX 레버리지", 30000, 90000, venture="8"),   # 레버리지 ETF
    row("100120", "ETN상품", 10000, 9000, market="A", venture="E"),
    row("100130", "동전주", 500, 9000),                          # 최소 주가 미달
    row("100140", "또다른회사", 10000, 4000),
]


def test_prefilter():
    out = universe.prefilter(MASTER, min_price=1000, max_price=50000, min_cap_eok=3000, include_etf=False)
    assert [o["code"] for o in out] == ["100010", "100090", "100140"]          # 시총 큰 순
    out = universe.prefilter(MASTER, min_price=1000, max_price=50000, min_cap_eok=3000, include_etf=True)
    codes = [o["code"] for o in out]
    assert "100100" in codes and "100110" not in codes                         # 레버리지는 항상 제외


class ScanBroker(FakeBroker):
    VALUES = {"100010": 50e8, "100090": 300e8, "100140": 120e8, "000001": 999e8}

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.scan_calls = 0

    def scan_quote(self, code):
        self.scan_calls += 1
        return {"price": 20000, "prev_value": self.VALUES.get(code, 0), "warnings": []}


def test_scan_ranks_by_prev_value_and_skips_core():
    cfg = make_cfg(Path(tempfile.mkdtemp()), auto_count=2)
    cfg.symbols = ["000001", "100010"]                         # 핵심 종목은 자동 후보에서 빠짐
    universe.load_stock_master = lambda: MASTER
    j = Journal(cfg.db_path)
    r = universe.scan(cfg, ScanBroker({}), j)
    assert [i["code"] for i in r["items"]] == ["100090", "100140"]    # 거래대금 300억 > 120억
    assert universe.saved(j)["items"][0]["prev_value_eok"] == 300.0


def test_ensure_today_scans_once_and_falls_back():
    cfg = make_cfg(Path(tempfile.mkdtemp()), auto_count=2)
    universe.load_stock_master = lambda: MASTER
    j, br = Journal(cfg.db_path), ScanBroker({})
    universe.ensure_today(cfg, br, j)
    n = br.scan_calls
    universe.ensure_today(cfg, br, j)
    assert br.scan_calls == n                                       # 같은 날 두 번째는 저장본 사용

    old = universe.saved(j)
    old["date"] = "2000-01-01"
    j.set(universe.STATE_KEY, __import__("json").dumps(old))
    universe.load_stock_master = lambda: (_ for _ in ()).throw(RuntimeError("마스터 다운로드 실패"))
    msgs = []
    items = universe.ensure_today(cfg, br, j, say=msgs.append)
    assert [i["code"] for i in items] == ["100090", "100140"] and "직전 목록" in msgs[-1]


def test_bot_includes_auto_candidates():
    tmp = Path(tempfile.mkdtemp())
    cfg = make_cfg(tmp, auto_count=2)
    universe.load_stock_master = lambda: MASTER
    prices = {"000001": 20000, "000002": 30000, "000003": 80000, "100090": 30000, "100140": 10000, "100010": 20000}
    br = ScanBroker(prices)
    ai = FakeAI([])
    out = Bot(cfg, br, ai, Journal(cfg.db_path)).run()
    src = {c["code"]: c["source"] for c in ai.ctx["candidates"]}
    assert src["000001"] == "core" and src["100090"] == "auto" and src["100140"] == "auto", out
    assert "자동 선정 2종목" in out


def test_market_closed_skips_scan():
    tmp = Path(tempfile.mkdtemp())
    cfg = make_cfg(tmp, auto_count=2)
    universe.load_stock_master = lambda: MASTER
    br = ScanBroker({"000001": 20000, "000002": 30000, "000003": 80000}, status="2")
    Bot(cfg, br, FakeAI([]), Journal(cfg.db_path)).run()
    assert br.scan_calls == 0                                       # 휴장/장외엔 검색도 안 함


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
