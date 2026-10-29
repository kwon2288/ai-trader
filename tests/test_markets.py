"""국내/미국 시장 분리 · NH 미국주식 · 토스증권 오프라인 테스트."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.update(NHPLUG_BASE_URL="https://moapi.nhplug.com:8443", NHPLUG_ACCOUNT="50001234567")

import nhplug  # noqa: E402

from test_bot import FakeAI, FakeBroker, make_cfg  # noqa: E402
from trader import analyst  # noqa: E402
from trader.bot import Bot  # noqa: E402
from trader.broker import BrokerError, Holding, KST  # noqa: E402
from trader.config import norm_symbol  # noqa: E402
from trader.journal import Journal  # noqa: E402


# ── 설정 ─────────────────────────────────────────────────
def us_cfg(tmp=None, **kw):
    cfg = make_cfg(Path(tmp or tempfile.mkdtemp()), **kw)
    cfg.us = {"enabled": True, "total_usd": 300, "max_per_position_usd": 150, "max_positions": 2,
              "symbols": ["aapl", "brk.b", "F"]}
    return cfg


def test_config_for_market():
    cfg = us_cfg()
    u = cfg.for_market("us")
    assert (u.market, u.currency, u.total_krw, u.max_per_position_krw) == ("us", "USD", 300.0, 150.0)
    assert u.symbols == ["AAPL", "BRK.B", "F"] and u.auto_count == 0 and u.order_type == "limit"
    assert cfg.ledger == "mock" and u.ledger == "mock_us"
    assert u.money(12.5) == "$12.50" and cfg.money(12500) == "12,500원"
    assert norm_symbol("5930") == "005930" and norm_symbol("0000d0") == "0000D0"


def test_normalize_keeps_us_tickers():
    r = analyst.normalize({"decisions": [{"code": "aapl", "action": "BUY", "amount_krw": 120.5,
                                          "confidence": 0.7, "reason": "r"}]}, "x")
    assert r["decisions"][0]["code"] == "AAPL" and r["decisions"][0]["amount_krw"] == 120.5


# ── 봇: 미국 장부(달러·소수점) + 국내와 분리 ───────────────
class USBroker(FakeBroker):
    def quote(self, code):
        q = super().quote(code)
        q.price = q.ask = self.prices[code]
        q.bid = round(self.prices[code] - 0.01, 2)
        return q


def test_bot_us_ledger_is_separate_and_fractional_cash():
    tmp = tempfile.mkdtemp()
    cfg = us_cfg(tmp).for_market("us")
    cfg.symbols = ["AAPL", "F", "PLTR"]
    br = USBroker({"AAPL": 231.4, "F": 10.37, "PLTR": 72.15})
    j = Journal(cfg.db_path)
    out = Bot(cfg, br, FakeAI([
        {"code": "F", "action": "BUY", "amount_krw": 100, "confidence": 0.8, "reason": "r"},
        {"code": "AAPL", "action": "BUY", "amount_krw": 150, "confidence": 0.9, "reason": "r"},  # $231 > 한도
    ]), j).run()
    assert ("BUY", "F", 9) in br.orders, out                  # 100 // 10.37 = 9주
    assert not any(o[1] == "AAPL" for o in br.orders)
    assert float(j.get("bot_cash_mock_us")) == round(300 - 9 * 10.37, 2)
    assert j.get("bot_cash_mock") is None                     # 국내 장부는 그대로
    assert "$" in out and "🇺🇸" in out
    assert j.bot_codes("us") == {"F": j.bot_codes("us")["F"]} and j.bot_codes("kr") == {}

    # 국내 실행이 미국 보유 종목 장부를 지우지 않는다
    kcfg = make_cfg(Path(tmp))
    Bot(kcfg, FakeBroker({"000001": 20000, "000002": 30000, "000003": 80000}), FakeAI([]), j).run()
    assert "F" in j.bot_codes("us")


# ── NH 미국주식 ───────────────────────────────────────────
def test_nh_us_broker_fields_and_routing():
    from trader.nh_us import NHUSBroker
    seen = []

    def call(path, body):
        seen.append((path, os.environ["NHPLUG_BASE_URL"], body))
        if path == "/gbstock/quote/v1/current":
            return {"Output_0": {"iem_nm": "애플", "trdprc": "231.4", "pctchng": "1.2", "best_ask1": 231.45,
                                 "best_bid1": 231.40, "marketperiod_cls": "1", "currency_prc": "1400",
                                 "exch_name": "NASDAQ"}}
        if path == "/gbstock/quote/v1/period":
            return {"Output_1": [{"trade_date": "20261028", "close_prc": "231.4", "open_prc": 230, "high": 232,
                                  "low": 229, "movolume": 1000},
                                 {"trade_date": "20261027", "close_prc": "228.0", "open_prc": 227, "high": 229,
                                  "low": 226, "movolume": 900}]}
        if path == "/gbstock/inquiry/v1/balance":
            return {"Output_0": {"fc_dca": 0}, "Output_1": [
                {"iem_cd": "f", "iem_nm": "포드", "cns_bse_bnc_qty": "9", "fc_avg_phs_pr": "10.37", "fc_sec_end_pr": "10.9"}]}
        if path == "/gbstock/inquiry/v1/buyableAmount":
            return {"Output_0": {"orr_pbl_amt": "1400000", "wtm_cur_cd": "KRW", "orr_pbl_qty": "6", "sll_pbl_qty": "9"}}
        return {"Output_0": {"orr_no": 777}, "rsp_msg": "정상"}

    nhplug.call = call
    b0 = NHUSBroker("mock", margin_currency="krw")
    assert b0.balance().orderable_cash == 1000.0                 # 시세 조회 전이라도 환율을 받아 환산
    b = NHUSBroker("mock", margin_currency="krw")
    q = b.quote("AAPL")
    assert (q.price, q.ask, q.market_status, b.fx) == (231.4, 231.45, "1", 1400.0)
    assert [x["date"] for x in b.daily("AAPL")] == ["20261027", "20261028"]
    bal = b.balance()
    assert bal.holdings["F"].qty == 9 and bal.holdings["F"].avg_price == 10.37
    assert bal.orderable_cash == 1000.0                          # 1,400,000원 / 1400
    assert b.buyable_qty("AAPL", 231.45) == 6 and b.sellable_qty("F") == 9
    r = b.buy("AAPL", 1, 231.456)
    assert r.ok and r.order_no == "777"
    order = [x for x in seen if x[0] == "/gbstock/order/v1/buy"][0]
    assert order[1].startswith("https://moapi.")                 # 주문은 모의 서버
    assert order[2]["fc_orr_uit_pr"] == 231.46 and order[2]["wtm_cur_knd_cd"] == "2" \
        and order[2]["fc_sec_trd_nat_cd"] == "200" and order[2]["ahi_nmn_pr_tp_cd"] == "00"
    assert all(u.startswith("https://api.") for p, u, _ in seen if p.startswith("/gbstock/quote/"))
    assert os.environ["NHPLUG_BASE_URL"].startswith("https://moapi.")


# ── 토스증권 ─────────────────────────────────────────────
class R:
    def __init__(self, status, body, headers=None):
        self.status_code, self._b, self.headers = status, body, headers or {}
        self.content, self.text = b"x", json.dumps(body, ensure_ascii=False)

    def json(self):
        return self._b


class TossSession:
    def __init__(self, open_now=True):
        self.calls, self.tokens, self.fail401 = [], 0, False
        now = datetime.now(KST)
        a, b = (now - timedelta(hours=1), now + timedelta(hours=1)) if open_now else \
               (now + timedelta(hours=5), now + timedelta(hours=6))
        self.session = {"startTime": a.isoformat(), "endTime": b.isoformat()}

    def post(self, url, data=None, timeout=None):
        self.tokens += 1
        return R(200, {"access_token": f"T{self.tokens}", "token_type": "Bearer", "expires_in": 86400})

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):  # noqa: A002
        path = url.split("tossinvest.com")[1]
        self.calls.append((method, path, params, json, headers))
        if self.fail401:
            self.fail401 = False
            return R(401, {"error": {"code": "unauthorized", "message": "expired"}})
        res = {
            "/api/v1/accounts": [{"accountNo": "123-45", "accountSeq": 7, "accountType": "BROKERAGE"}],
            "/api/v1/prices": [{"symbol": "AAPL", "lastPrice": "231.40", "currency": "USD"}],
            "/api/v1/candles": {"candles": [{"timestamp": "2026-10-28T00:00:00-04:00", "openPrice": "230",
                                             "highPrice": "232", "lowPrice": "229", "closePrice": "231.40", "volume": "10"},
                                            {"timestamp": "2026-10-27T00:00:00-04:00", "openPrice": "227",
                                             "highPrice": "229", "lowPrice": "226", "closePrice": "228.00", "volume": "9"}]},
            "/api/v1/stocks": [{"symbol": "AAPL", "name": "애플"}],
            "/api/v1/market-calendar/US": {"today": {"date": "2026-10-28", "regularMarket": self.session},
                                           "previousBusinessDay": {"date": "2026-10-27", "regularMarket": None}},
            "/api/v1/holdings": {"items": [
                {"symbol": "F", "name": "포드", "marketCountry": "US", "quantity": "9", "averagePurchasePrice": "10.37",
                 "lastPrice": "10.90"},
                {"symbol": "005930", "name": "삼성전자", "marketCountry": "KR", "quantity": "1",
                 "averagePurchasePrice": "70000", "lastPrice": "71000"}]},
            "/api/v1/buying-power": {"currency": "USD", "cashBuyingPower": "512.34"},
            "/api/v1/sellable-quantity": {"sellableQuantity": "9"},
            "/api/v1/orders": {"orderId": "abcdef1234567890xyz", "clientOrderId": "x"},
        }.get(path)
        return R(200, {"result": res})


def make_toss(mode="live", **kw):
    from trader import toss
    toss.INTERVAL.update({k: 0 for k in toss.INTERVAL})
    os.environ.update(TOSS_CLIENT_ID="cid", TOSS_CLIENT_SECRET="sec")
    os.environ.pop("TOSS_ACCOUNT_SEQ", None)
    tmp = Path(tempfile.mkdtemp())
    sess = TossSession(**kw)
    orig = toss.requests.Session
    toss.requests.Session = lambda: sess
    try:
        b = toss.TossBroker(mode, "us", tmp)
    finally:
        toss.requests.Session = orig
    return b, sess, tmp


def test_toss_rejects_mock_mode():
    from trader import toss
    try:
        toss.TossBroker("mock", "kr", Path(tempfile.mkdtemp()))
    except BrokerError as e:
        assert "모의투자" in str(e)
        return
    raise AssertionError


def test_toss_us_flow():
    b, s, tmp = make_toss()
    assert b.account_seq == "7"
    q = b.quote("AAPL")
    assert (q.name, q.price, q.market_status) == ("애플", 231.4, "1")
    assert round(q.change_pct, 2) == round((231.4 / 228 - 1) * 100, 2)
    assert [x["date"] for x in b.daily("AAPL")] == ["20261027", "20261028"]   # 오래된 순으로 뒤집힘
    bal = b.balance()
    assert list(bal.holdings) == ["F"] and bal.orderable_cash == 512.34      # 미국 종목만
    assert b.buyable_qty("AAPL", 231.4) == 2 and b.sellable_qty("F") == 9
    r = b.buy("AAPL", 2, 231.456)
    order = [c for c in s.calls if c[0] == "POST"][0]
    assert r.ok and order[3]["price"] == "231.46" and order[3]["orderType"] == "LIMIT" \
        and order[3]["quantity"] == "2" and order[4]["X-Tossinvest-Account"] == "7"
    assert s.tokens == 1 and json.loads((tmp / "toss_token.json").read_text())["token"] == "T1"


def test_toss_dry_never_orders_and_closed_market():
    b, s, _ = make_toss(mode="dry", open_now=False)
    assert b.buy("AAPL", 1).order_no == "DRY" and not any(c[0] == "POST" for c in s.calls)
    assert b.quote("AAPL").market_status == ""


def test_toss_reissues_token_on_401():
    b, s, _ = make_toss()
    s.fail401 = True
    assert b.quote("AAPL").price == 231.4
    assert s.tokens == 2


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
