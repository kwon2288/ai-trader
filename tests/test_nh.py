"""NH 래퍼 오프라인 테스트 — 모의투자에서 시세만 실전 서버로, 주문·잔고는 모의 서버로 가는지."""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.update(NHPLUG_BASE_URL="https://moapi.nhplug.com:8443", NHPLUG_ACCOUNT="50001234567")

import nhplug  # noqa: E402

from trader import broker as B  # noqa: E402


def test_quote_routed_to_live_orders_stay_mock():
    seen = []

    def call(path, body):
        url = os.environ["NHPLUG_BASE_URL"]
        seen.append((path, url))
        if path.startswith("/krstock/quote/") and "moapi" in url:
            raise RuntimeError("IGW40023 모의투자에서는 제공하지 않는 API")
        if path.endswith("currentPrice"):
            return {"Output_0": {"iem_nm": "KODEX 200", "stck_prpr": "40000", "market_status": "1"}}
        if path.endswith("currentDaily"):
            return {"Output_0": [{"bsop_date": "26/10/27", "stck_clpr": 40000}]}
        if path.endswith("balance"):
            return {"Output_0": {"orr_pbl_amt4": 1_250_000_000}, "Output_1": []}
        return {"Output_0": {"mkt_orr_no": 1}}

    nhplug.call = call
    b = B.NHBroker("mock")
    assert b.quote("069500").price == 40000 and b.daily("069500")[0]["date"] == "20261027"
    b.balance()
    b.buy("069500", 1)
    b.sell("069500", 1)
    for path, url in seen:
        if path.startswith("/krstock/quote/"):
            assert url.startswith("https://api."), (path, url)
        else:
            assert url.startswith("https://moapi."), (path, url)   # 주문·잔고는 절대 실전으로 안 감
    assert os.environ["NHPLUG_BASE_URL"].startswith("https://moapi.")


def test_url_restored_after_quote_error():
    def call(path, body):
        raise RuntimeError("network")
    nhplug.call = call
    b = B.NHBroker("mock")
    try:
        b.quote("069500")
    except RuntimeError:
        pass
    assert os.environ["NHPLUG_BASE_URL"].startswith("https://moapi.")


def test_mode_decides_server_and_account():
    seen = []
    accts = [{"acct_no": "50001234567", "acct_type": "03"}, {"acct_no": "70001111111", "acct_type": "01"}]

    def call(path, body):
        seen.append((path, os.environ["NHPLUG_BASE_URL"], body))
        if path == "/n2/acctinfo":
            return {"Output_0": accts}
        return {"Output_0": {"mkt_orr_no": 1}}

    nhplug.call = call
    live = B.NHBroker("live")          # .env 는 moapi 지만 실계좌 모드면 실전 서버 + 실계좌
    assert live.env == "live" and live.account == "70001111111"
    seen.clear()
    live.buy("069500", 1, 40000)
    assert seen[-1][1].startswith("https://api.") and seen[-1][2]["act_no"] == "70001111111"
    mock = B.NHBroker("mock")
    assert mock.env == "mock" and mock.account == "50001234567"
    assert os.environ["NHPLUG_BASE_URL"].startswith("https://moapi.")   # .env 값은 그대로


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
