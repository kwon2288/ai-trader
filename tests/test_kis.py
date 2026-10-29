"""KIS 래퍼 오프라인 테스트 — HTTP 응답을 흉내 내서 파싱·tr_id·안전장치를 검증한다."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trader import kis  # noqa: E402
from trader.broker import BrokerError  # noqa: E402
from trader.journal import KST  # noqa: E402

TODAY = datetime.now(KST).strftime("%Y%m%d")


class Resp:
    def __init__(self, body, status=200):
        self._b, self.status_code = body, status
        self.content, self.text = b"x", json.dumps(body, ensure_ascii=False)

    def json(self):
        return self._b


class FakeSession:
    def __init__(self):
        self.calls = []

    def post(self, url, headers=None, data=None, json=None, timeout=None):  # noqa: A002
        if url.endswith("/oauth2/tokenP"):
            self.calls.append(("TOKEN", url))
            return Resp({"access_token": "tok", "access_token_token_expired": "2099-01-01 00:00:00"})
        self.calls.append(("POST", url, headers["tr_id"], data))
        return Resp({"rt_cd": "0", "msg1": "주문 전송 완료", "output": {"ODNO": "0000123"}})

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append(("GET", url, headers["tr_id"]))
        if "itemchartprice" in url:
            return Resp({"rt_cd": "0", "output1": {"hts_kor_isnm": "카카오"}, "output2": [
                {"stck_bsop_date": TODAY, "stck_clpr": "41250", "acml_vol": "10"},
                {"stck_bsop_date": "20260101", "stck_clpr": "40000", "acml_vol": "9"}]})
        if "inquire-price" in url:
            return Resp({"rt_cd": "0", "output": {"stck_prpr": "41250", "prdy_ctrt": "1.5",
                         "short_over_yn": "Y", "mrkt_warn_cls_code": "00", "rprs_mrkt_kor_name": "KOSPI"}})
        if "inquire-balance" in url:
            return Resp({"rt_cd": "0", "output1": [
                {"pdno": "035720", "prdt_name": "카카오", "hldg_qty": "2", "ord_psbl_qty": "2",
                 "pchs_avg_pric": "40000.0000", "prpr": "41250"}],
                "output2": [{"dnca_tot_amt": "500000", "prvs_rcdl_excc_amt": "420000"}]})
        if "psbl-order" in url:
            return Resp({"rt_cd": "0", "output": {"nrcvb_buy_qty": "7"}})
        return Resp({"rt_cd": "1", "msg_cd": "X", "msg1": "모르는 요청"})


def make(mode="mock", server="mock"):
    os.environ.update(KIS_SERVER=server, KIS_APP_KEY="k", KIS_APP_SECRET="s", KIS_ACCOUNT="12345678-01")
    b = kis.KISBroker(mode, Path(tempfile.mkdtemp()))
    b.session = FakeSession()
    kis.INTERVAL.update(mock=0, live=0)
    return b


def test_parsing_and_mock_tr_ids():
    b = make()
    q = b.quote("035720")
    assert (q.name, q.price, q.warnings) == ("카카오", 41250, ["단기과열"])
    assert [x["date"] for x in b.daily("035720")] == ["20260101", TODAY]
    bal = b.balance()
    h = bal.holdings["035720"]
    assert (bal.orderable_cash, h.qty, h.avg_price) == (420000, 2, 40000)
    assert b.buyable_qty("035720", 41250) == 7 and b.sellable_qty("035720") == 2
    r = b.buy("035720", 1)
    assert r.ok and r.order_no == "0000123"
    post = [c for c in b.session.calls if c[0] == "POST"][0]
    assert post[2] == "VTTC0012U"                       # 모의투자 매수 tr_id
    body = json.loads(post[3])
    assert body["ORD_DVSN"] == "01" and body["ORD_UNPR"] == "0" and body["CANO"] == "12345678"
    tr_ids = {c[2] for c in b.session.calls if c[0] == "GET"}
    assert "VTTC8434R" in tr_ids and "FHKST01010100" in tr_ids
    assert sum(1 for c in b.session.calls if c[0] == "TOKEN") == 1   # 토큰 1회만 발급


def test_live_tr_ids_and_dry_never_posts():
    b = make("live", "live")
    b.sell("035720", 1, 41000)
    post = [c for c in b.session.calls if c[0] == "POST"][0]
    assert post[2] == "TTTC0011U" and json.loads(post[3])["ORD_DVSN"] == "00"
    d = make("dry", "live")
    assert d.buy("035720", 1).order_no == "DRY"
    assert not any(c[0] == "POST" for c in d.session.calls)


def test_mode_decides_server():
    # 웹에서 모드를 바꿔도 되도록 서버는 모드가 정한다 (KIS_SERVER 는 dry 일 때만)
    assert make("mock", "live").env == "mock"
    assert make("live", "mock").env == "live"
    assert make("dry", "live").env == "live"
    os.environ.update(KIS_LIVE_APP_KEY="LK", KIS_LIVE_ACCOUNT="87654321-01")
    try:
        b = make("live", "mock")
        assert b.app_key == "LK"
    finally:
        for k in ("KIS_LIVE_APP_KEY", "KIS_LIVE_ACCOUNT"):
            os.environ.pop(k, None)


def test_bad_account_rejected():
    os.environ["KIS_ACCOUNT"] = "1234"
    try:
        kis.KISBroker("dry", Path(tempfile.mkdtemp()))
    except BrokerError:
        return
    raise AssertionError


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
