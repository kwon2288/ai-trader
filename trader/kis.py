"""한국투자증권(KIS) Open API 래퍼.

엔드포인트·tr_id·필드명은 공식 예제(github.com/koreainvestment/open-trading-api, examples_llm) 기준.
  실전: https://openapi.koreainvestment.com:9443
  모의: https://openapivts.koreainvestment.com:29443  (모의투자 전용 앱키 필요)
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

from .broker import Balance, BrokerError, Holding, OrderResult, Quote, _int, _num
from .journal import KST

log = logging.getLogger(__name__)

URLS = {
    "live": "https://openapi.koreainvestment.com:9443",
    "mock": "https://openapivts.koreainvestment.com:29443",
}
# 초당 호출 제한 대응 (공식 예제와 동일: 실전 0.05s, 모의 0.5s)
INTERVAL = {"live": 0.06, "mock": 0.5}


class KISBroker:
    """mode: dry | mock | live. dry 는 조회만 하고 주문은 절대 내지 않는다."""

    def __init__(self, mode: str, token_dir: Path, account: str | None = None):
        self.mode = mode
        # 서버는 모드로 정한다(dry 는 KIS_SERVER 를 따름). KIS 는 모의/실전 앱키가 다르므로
        # KIS_MOCK_APP_KEY / KIS_LIVE_APP_KEY 처럼 환경별 값이 있으면 그걸 먼저 쓴다.
        self.env = mode if mode in URLS else os.environ.get("KIS_SERVER", "mock").strip().lower()
        if self.env not in URLS:
            raise BrokerError("KIS_SERVER 는 mock 또는 live 여야 합니다")
        E = self.env.upper()

        self.base = URLS[self.env]
        self.app_key = (os.environ.get(f"KIS_{E}_APP_KEY") or os.environ.get("KIS_APP_KEY", "")).strip()
        self.app_secret = (os.environ.get(f"KIS_{E}_APP_SECRET") or os.environ.get("KIS_APP_SECRET", "")).strip()
        acct = (account or os.environ.get(f"KIS_{E}_ACCOUNT") or os.environ.get("KIS_ACCOUNT", "")).replace("-", "").strip()
        if not (self.app_key and self.app_secret):
            raise BrokerError(".env 에 KIS_APP_KEY / KIS_APP_SECRET 을 넣으세요")
        if len(acct) != 10 or not acct.isdigit():
            raise BrokerError("KIS_ACCOUNT 는 '12345678-01' 형식(계좌 8자리 + 상품코드 2자리)이어야 합니다")
        self.cano, self.prdt = acct[:8], acct[8:]
        self.account = f"{self.cano}-{self.prdt}"

        self.token_file = token_dir / f"kis_token_{self.env}.json"
        self.session = requests.Session()
        self._last_call = 0.0
        self._token: str | None = None
        self._cache: dict[str, dict] = {}   # code → {name, bars}

    # ── 인증 (24시간 토큰, 파일 캐시 — 재발급은 분당 1회 제한) ──
    def _get_token(self) -> str:
        if self._token:
            return self._token
        try:
            saved = json.loads(self.token_file.read_text())
            if saved.get("app_key") == self.app_key and \
                    datetime.fromisoformat(saved["expires"]) > datetime.now(KST) + timedelta(minutes=10):
                self._token = saved["token"]
                return self._token
        except (OSError, ValueError, KeyError):
            pass
        r = self.session.post(f"{self.base}/oauth2/tokenP", timeout=15, json={
            "grant_type": "client_credentials", "appkey": self.app_key, "appsecret": self.app_secret})
        body = r.json() if r.content else {}
        if r.status_code != 200 or not body.get("access_token"):
            raise BrokerError(f"토큰 발급 실패 ({r.status_code}): {body.get('error_description') or r.text[:200]}")
        exp = body.get("access_token_token_expired", "")
        try:
            expires = datetime.strptime(exp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)
        except ValueError:
            expires = datetime.now(KST) + timedelta(hours=23)
        self.token_file.write_text(json.dumps({
            "token": body["access_token"], "expires": expires.isoformat(), "app_key": self.app_key}))
        os.chmod(self.token_file, 0o600)
        self._token = body["access_token"]
        return self._token

    # ── 공통 호출 ──
    def _req(self, method: str, path: str, tr_id: str, params: dict) -> dict:
        wait = INTERVAL[self.env] - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self._get_token()}",
            "appkey": self.app_key, "appsecret": self.app_secret,
            "tr_id": tr_id, "custtype": "P", "tr_cont": "",
        }
        url = self.base + path
        if method == "GET":
            r = self.session.get(url, headers=headers, params=params, timeout=15)
        else:
            r = self.session.post(url, headers=headers, data=json.dumps(params), timeout=15)
        self._last_call = time.monotonic()
        try:
            body = r.json()
        except ValueError:
            raise BrokerError(f"{path} HTTP {r.status_code}: {r.text[:200]}") from None
        if r.status_code != 200 or str(body.get("rt_cd")) != "0":
            raise BrokerError(f"{path} 실패: [{body.get('msg_cd', r.status_code)}] {body.get('msg1', '')}".strip())
        return body

    def _tr(self, real_id: str) -> str:
        """거래(T…) tr_id 는 모의투자에서 V… 로 바뀐다."""
        return "V" + real_id[1:] if self.env == "mock" and real_id[0] in "TJC" else real_id

    # ── 시세 ──
    def _load_chart(self, code: str) -> dict:
        if code not in self._cache:
            today = datetime.now(KST)
            body = self._req("GET", "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
                             "FHKST03010100", {
                                 "FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code,
                                 "FID_INPUT_DATE_1": (today - timedelta(days=150)).strftime("%Y%m%d"),
                                 "FID_INPUT_DATE_2": today.strftime("%Y%m%d"),
                                 "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "0"})
            bars = [{
                "date": str(r.get("stck_bsop_date", "")),
                "open": _int(r.get("stck_oprc")), "high": _int(r.get("stck_hgpr")),
                "low": _int(r.get("stck_lwpr")), "close": _int(r.get("stck_clpr")),
                "volume": _int(r.get("acml_vol")),
            } for r in (body.get("output2") or []) if _int(r.get("stck_clpr")) > 0]
            bars.sort(key=lambda b: b["date"])
            name = ((body.get("output1") or {}).get("hts_kor_isnm") or code).strip()
            self._cache[code] = {"name": name, "bars": bars}
        return self._cache[code]

    @staticmethod
    def _market_open(bars: list[dict]) -> bool:
        """정규장(09:00~15:20) + 오늘 일봉 존재(=휴장일 아님)."""
        now = datetime.now(KST)
        if now.weekday() >= 5 or not (9 * 60 <= now.hour * 60 + now.minute <= 15 * 60 + 20):
            return False
        return bool(bars) and bars[-1]["date"] == now.strftime("%Y%m%d")

    def quote(self, code: str) -> Quote:
        chart = self._load_chart(code)
        o = self._req("GET", "/uapi/domestic-stock/v1/quotations/inquire-price", "FHKST01010100",
                      {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code}).get("output") or {}
        price = _int(o.get("stck_prpr"))
        if not price:
            raise BrokerError(f"{code} 현재가 없음")
        warns = []
        if (o.get("invt_caful_yn") or "").strip() == "Y":
            warns.append("투자유의")
        if (o.get("mrkt_warn_cls_code") or "00").strip() not in {"", "00"}:
            warns.append("투자주의/경고/위험")
        if (o.get("short_over_yn") or "").strip() == "Y":
            warns.append("단기과열")
        if (o.get("sltr_yn") or "").strip() == "Y":
            warns.append("정리매매")
        if (o.get("mang_issu_cls_code") or "").strip() == "Y":
            warns.append("관리종목")
        if (o.get("temp_stop_yn") or "").strip() == "Y":
            warns.append("거래정지")
        return Quote(
            code=code, name=chart["name"], price=price, change_pct=_num(o.get("prdy_ctrt")),
            ask=price, bid=price,  # 현재가 API엔 호가가 없어 현재가로 추정
            market_status="1" if self._market_open(chart["bars"]) else "",
            market=(o.get("rprs_mrkt_kor_name") or "").strip(),
            sector=(o.get("bstp_kor_isnm") or "").strip(),
            per=_num(o.get("per")), pbr=_num(o.get("pbr")), foreign_pct=_num(o.get("hts_frgn_ehrt")),
            w52_high=_int(o.get("w52_hgpr")), w52_low=_int(o.get("w52_lwpr")), warnings=warns,
        )

    def daily(self, code: str, n: int = 80) -> list[dict]:
        return self._load_chart(code)["bars"][-n:]

    # ── 계좌 ──
    def _balance_raw(self) -> dict:
        return self._req("GET", "/uapi/domestic-stock/v1/trading/inquire-balance", self._tr("TTTC8434R"), {
            "CANO": self.cano, "ACNT_PRDT_CD": self.prdt, "AFHR_FLPR_YN": "N", "OFL_YN": "",
            "INQR_DVSN": "02", "UNPR_DVSN": "01", "FUND_STTL_ICLD_YN": "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "00", "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""})

    def balance(self) -> Balance:
        body = self._balance_raw()
        holdings = {}
        for r in body.get("output1") or []:
            qty = _int(r.get("hldg_qty"))
            if qty > 0:
                code = str(r.get("pdno", "")).strip()
                holdings[code] = Holding(code, (r.get("prdt_name") or "").strip(), qty,
                                         round(_num(r.get("pchs_avg_pric"))), _int(r.get("prpr")))
        o2 = body.get("output2") or [{}]
        s = o2[0] if isinstance(o2, list) and o2 else (o2 if isinstance(o2, dict) else {})
        cash = _int(s.get("prvs_rcdl_excc_amt")) or _int(s.get("dnca_tot_amt"))
        return Balance(orderable_cash=cash, holdings=holdings)

    def buyable_qty(self, code: str, price: int) -> int:
        o = self._req("GET", "/uapi/domestic-stock/v1/trading/inquire-psbl-order", self._tr("TTTC8908R"), {
            "CANO": self.cano, "ACNT_PRDT_CD": self.prdt, "PDNO": code, "ORD_UNPR": str(price),
            "ORD_DVSN": "00", "CMA_EVLU_AMT_ICLD_YN": "N", "OVRS_ICLD_YN": "N"}).get("output") or {}
        return _int(o.get("nrcvb_buy_qty"))

    def sellable_qty(self, code: str) -> int:
        for r in self._balance_raw().get("output1") or []:
            if str(r.get("pdno", "")).strip() == code:
                return _int(r.get("ord_psbl_qty"))
        return 0

    # ── 주문 ──
    def _order(self, side: str, code: str, qty: int, price: int | None) -> OrderResult:
        if self.mode == "dry":
            return OrderResult(True, "DRY", f"[드라이런] {side} {code} {qty}주 (전송 안 함)")
        body = {
            "CANO": self.cano, "ACNT_PRDT_CD": self.prdt, "PDNO": code,
            "ORD_DVSN": "01" if price is None else "00",       # 01 시장가 / 00 지정가
            "ORD_QTY": str(qty), "ORD_UNPR": "0" if price is None else str(price),
            "EXCG_ID_DVSN_CD": "KRX", "SLL_TYPE": "01" if side == "SELL" else "", "CNDT_PRIC": "",
        }
        tr = self._tr("TTTC0012U" if side == "BUY" else "TTTC0011U")
        try:
            res = self._req("POST", "/uapi/domestic-stock/v1/trading/order-cash", tr, body)
        except BrokerError as e:
            return OrderResult(False, "", str(e))
        out = res.get("output") or {}
        odno = str(out.get("ODNO", "")).strip()
        org = str(out.get("KRX_FWDG_ORD_ORGNO", "")).strip()
        return OrderResult(bool(odno), f"{odno}|{org}" if org else odno, res.get("msg1", ""))   # 취소에 지점번호 필요

    def buy(self, code: str, qty: int, price: int | None = None) -> OrderResult:
        return self._order("BUY", code, qty, price)

    def sell(self, code: str, qty: int, price: int | None = None) -> OrderResult:
        return self._order("SELL", code, qty, price)

    # ── 체결 확인 · 취소 ──
    def order_status(self, order_no: str, code: str, side: str, qty: int, price: float, day: str):
        from .broker import OrderFill
        odno = order_no.split("|")[0]
        d = day.replace("-", "")
        body = self._req("GET", "/uapi/domestic-stock/v1/trading/inquire-daily-ccld", self._tr("TTTC0081R"), {
            "CANO": self.cano, "ACNT_PRDT_CD": self.prdt, "INQR_STRT_DT": d, "INQR_END_DT": d,
            "SLL_BUY_DVSN_CD": "00", "PDNO": code, "ORD_GNO_BRNO": "", "ODNO": odno, "CCLD_DVSN": "00",
            "INQR_DVSN": "00", "INQR_DVSN_1": "", "INQR_DVSN_3": "00", "EXCG_ID_DVSN_CD": "KRX",
            "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""})
        rows = body.get("output1") or []
        r = next((x for x in rows if str(x.get("odno", "")).lstrip("0") == odno.lstrip("0")), None)
        if r is None:
            return None
        filled, open_q = _int(r.get("tot_ccld_qty")), _int(r.get("rmn_qty"))
        canceled = str(r.get("cncl_yn", "")).upper() == "Y" or _int(r.get("cncl_cfrm_qty")) > 0
        rej = _int(r.get("rjct_qty")) > 0
        state = ("rejected" if rej and not filled else "filled" if filled >= int(qty)
                 else "canceled" if canceled else "partial" if filled else "open")
        return OrderFill(filled, float(r.get("avg_prvs") or 0), open_q, state)

    def cancel(self, order_no: str, code: str) -> OrderResult:
        if self.mode == "dry":
            return OrderResult(True, "DRY", "[드라이런] 취소")
        odno, _, org = order_no.partition("|")
        try:
            res = self._req("POST", "/uapi/domestic-stock/v1/trading/order-rvsecncl", self._tr("TTTC0013U"), {
                "CANO": self.cano, "ACNT_PRDT_CD": self.prdt, "KRX_FWDG_ORD_ORGNO": org, "ORGN_ODNO": odno,
                "ORD_DVSN": "00", "RVSE_CNCL_DVSN_CD": "02", "ORD_QTY": "0", "ORD_UNPR": "0",
                "QTY_ALL_ORD_YN": "Y", "EXCG_ID_DVSN_CD": "KRX"})
        except BrokerError as e:
            return OrderResult(False, "", str(e))
        no = str((res.get("output") or {}).get("ODNO", "")).strip()
        return OrderResult(bool(no), no, res.get("msg1", ""))
