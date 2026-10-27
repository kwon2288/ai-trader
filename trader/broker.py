"""공통 데이터 구조 + NH투자증권(나무) NHPLUG Open API 래퍼 — 기본 증권사.
한국투자증권은 trader/kis.py (BROKER=kis)

공식 SDK(`pip install nhplug`)의 call() 을 쓰고, 필드명은 공식 openapi.json(krstock) 기준.
SDK 는 성공/실패를 판정하지 않으므로 '기대한 값이 왔는지'를 직접 확인한다.
"""
from __future__ import annotations

import logging
import os
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)


def _num(v, default=0.0) -> float:
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return default


def _int(v, default=0) -> int:
    return int(_num(v, default))


KST = ZoneInfo("Asia/Seoul")


def _ymd(v) -> str:
    """'26/10/27', '2026-10-27', '20261027' → '20261027'."""
    d = re.sub(r"\D", "", str(v or ""))
    return "20" + d if len(d) == 6 else d


def in_regular_hours(now: datetime | None = None) -> bool:
    now = now or datetime.now(KST)
    return now.weekday() < 5 and 9 * 60 <= now.hour * 60 + now.minute <= 15 * 60 + 20


@dataclass
class Quote:
    code: str
    name: str
    price: int
    change_pct: float
    ask: int
    bid: int
    market_status: str          # 0.프리 1.정규 2.애프터 A.단일가 C.일시마감
    market: str                 # 코스피/코스닥/ETF ...
    sector: str
    per: float
    pbr: float
    foreign_pct: float
    w52_high: int
    w52_low: int
    warnings: list[str] = field(default_factory=list)  # 투자유의·경고·정리매매·단기과열 등


@dataclass
class Holding:
    code: str
    name: str
    qty: int
    avg_price: int
    now_price: int

    @property
    def value(self) -> int:
        return self.qty * self.now_price

    @property
    def pnl_pct(self) -> float:
        return (self.now_price / self.avg_price - 1) * 100 if self.avg_price else 0.0


@dataclass
class Balance:
    orderable_cash: int
    holdings: dict[str, Holding]


@dataclass
class OrderResult:
    ok: bool
    order_no: str
    message: str


class BrokerError(RuntimeError):
    pass


class NHBroker:
    """mode: dry | mock | live. dry 는 조회만 하고 주문은 절대 내지 않는다."""

    # 모의투자 서버는 시세 API 를 제공하지 않는다(IGW40023) → 시세만 실전 서버에서 조회.
    # 시세는 계좌와 무관한 공개 데이터라 안전하며, 주문·잔고는 절대 실전으로 가지 않는다.
    QUOTE_PREFIX = "/krstock/quote/"
    _url_lock = threading.Lock()

    def __init__(self, mode: str, account: str | None = None):
        from nhplug import call, get_base_url, status_of  # 지연 import (테스트에서 불필요)

        self._call, self._status_of = call, status_of
        self.mode = mode
        base = get_base_url()
        host = base.split("//")[-1].split("/")[0].lower()
        self.env = "mock" if host.startswith("moapi.") else "live"
        self._base = base
        self._quote_base = base.replace("://moapi.", "://api.", 1) if self.env == "mock" else base

        # 🔒 모드-도메인 불일치 차단: 모의투자 모드인데 실서버로 주문 나가는 사고 방지
        if mode == "mock" and self.env != "mock":
            raise BrokerError("TRADING_MODE=mock 인데 NHPLUG_BASE_URL 이 운영(api) 서버입니다. "
                              "moapi.nhplug.com:8443 으로 바꾸세요.")
        if mode == "live" and self.env != "live":
            raise BrokerError("TRADING_MODE=live 인데 NHPLUG_BASE_URL 이 모의투자(moapi) 서버입니다.")

        self.account = account or os.environ.get("NHPLUG_ACCOUNT") or self._pick_account()

    # ── 공통 ────────────────────────────────────────────
    def _post(self, path: str, body: dict) -> dict:
        if self._quote_base != self._base and path.startswith(self.QUOTE_PREFIX):
            with self._url_lock:  # 웹·타이머 스레드가 동시에 불러도 주소가 섞이지 않게
                os.environ["NHPLUG_BASE_URL"] = self._quote_base
                try:
                    data = self._call(path, body)
                finally:
                    os.environ["NHPLUG_BASE_URL"] = self._base
        else:
            with self._url_lock:
                data = self._call(path, body)
        return data if isinstance(data, dict) else {}

    def _msg(self, data: dict) -> str:
        try:
            return str(self._status_of(data)[1] or "")
        except Exception:  # noqa: BLE001
            return ""

    def _pick_account(self) -> str:
        want = {"03"} if self.env == "mock" else {"01", "02"}
        data = self._post("/n2/acctinfo", {})
        rows = data.get("Output_0") or []
        usable = [r["acct_no"] for r in rows if (r.get("acct_type") or "").strip() in want]
        if not usable:
            raise BrokerError(f"{self.env} 환경에서 쓸 수 있는 계좌가 없습니다 "
                              f"(모의투자는 nhplug.com 포털에서 모의투자 신청 필요). 서버 응답: {self._msg(data) or '-'}")
        if len(usable) == 1:
            return usable[0]
        # 계좌 목록엔 계좌 종류가 없으므로, 국내주식 잔고 조회가 되는 계좌만 남긴다
        ok = [a for a in usable if self._is_stock_account(a)]
        if len(ok) == 1:
            return ok[0]
        cand = ok or usable
        raise BrokerError(f"국내주식 계좌 후보가 여러 개입니다: {cand} → .env 에 NHPLUG_ACCOUNT=<계좌번호> 지정 "
                          "(나무 앱의 '국내주식' 계좌번호와 같은 것을 고르세요)")

    def _is_stock_account(self, act_no: str) -> bool:
        try:
            data = self._post("/krstock/inquiry/v1/balance", {
                "act_no": act_no, "bnc_bse_cd": "5", "ltg_aot_dit_cd": "9",
                "aet_bse": "2", "qut_dit_cd": "UNT", "aly_qut_cd": "1"})
        except Exception:  # noqa: BLE001
            return False
        return bool(data.get("Output_0"))

    def list_accounts(self) -> list[dict]:
        """check 명령용: 이 환경의 계좌와 국내주식 잔고 조회 가능 여부."""
        want = {"03"} if self.env == "mock" else {"01", "02"}
        rows = self._post("/n2/acctinfo", {}).get("Output_0") or []
        return [{"acct_no": r["acct_no"], "acct_type": r.get("acct_type"),
                 "stock_ok": self._is_stock_account(r["acct_no"])}
                for r in rows if (r.get("acct_type") or "").strip() in want]

    # ── 시세 ────────────────────────────────────────────
    def quote(self, code: str) -> Quote:
        data = self._post("/krstock/quote/v1/currentPrice", {"iem_cd": code, "market_cd": "KRX"})
        o = data.get("Output_0") or {}
        if not o.get("stck_prpr"):
            raise BrokerError(f"{code} 현재가 조회 실패: {self._msg(data)}")
        warns = []
        if (o.get("invt_epmd_yn") or "").strip() == "Y":
            warns.append("투자유의")
        if (o.get("mrkt_alrm_code") or "").strip() in {"1", "2", "3", "4"}:
            warns.append("투자주의/경고/위험")
        if (o.get("sltr_yn") or "").strip() == "Y":
            warns.append("정리매매")
        if (o.get("short_over_code") or "").strip() in {"1", "2", "3"}:
            warns.append("단기과열")
        return Quote(
            code=code,
            name=(o.get("iem_nm") or code).strip(),
            price=_int(o.get("stck_prpr")),
            change_pct=_num(o.get("prdy_ctrt")),
            ask=_int(o.get("askp1") or o.get("askp")),
            bid=_int(o.get("bidp1") or o.get("bidp")),
            market_status=self._market_status(code, o),
            market=(o.get("scrt_grp_isnm") or o.get("mrkt_div_isnm") or "").strip(),
            sector=(o.get("bstp_kor_isnm") or "").strip(),
            per=_num(o.get("per")),
            pbr=_num(o.get("pbr")),
            foreign_pct=_num(o.get("for_rate")),
            w52_high=_int(o.get("w52_hgpr")),
            w52_low=_int(o.get("w52_lwpr")),
            warnings=warns,
        )

    def _market_status(self, code: str, o: dict) -> str:
        """API 의 장상태(1=정규장)를 쓰되, 비어 오면 시간대 + 오늘 일봉 존재(=개장일)로 판단."""
        st = (o.get("market_status") or "").strip()
        if st:
            return st
        if not in_regular_hours():
            return ""
        bars = self.daily(code, 2)
        return "1" if bars and bars[-1]["date"] == datetime.now(KST).strftime("%Y%m%d") else ""

    def daily(self, code: str, n: int = 80) -> list[dict]:
        """일봉 (오래된 → 최신 순)."""
        data = self._post("/krstock/quote/v1/currentDaily", {
            "market_cd": "KRX", "iem_cd": code, "array_cnt": str(n), "view_main_yn": "Y",
        })
        rows = data.get("Output_0") or []
        out = [{
            "date": _ymd(r.get("bsop_date")),
            "open": _int(r.get("stck_oprc")), "high": _int(r.get("stck_hgpr")),
            "low": _int(r.get("stck_lwpr")), "close": _int(r.get("stck_clpr")),
            "volume": _int(r.get("acml_vol")),
        } for r in rows if _int(r.get("stck_clpr")) > 0]
        out.sort(key=lambda r: r["date"])
        return out

    # ── 계좌 ────────────────────────────────────────────
    def balance(self) -> Balance:
        data = self._post("/krstock/inquiry/v1/balance", {
            "act_no": self.account, "bnc_bse_cd": "5", "ltg_aot_dit_cd": "9",
            "aet_bse": "2", "qut_dit_cd": "UNT", "aly_qut_cd": "1",
        })
        o0 = data.get("Output_0")
        if not o0:
            raise BrokerError(f"잔고 조회 실패: {self._msg(data)}")
        holdings = {}
        for r in data.get("Output_1") or []:
            qty = _int(r.get("itg_bnc_qty") or r.get("rsdl_qty"))
            if qty <= 0:
                continue
            code = str(r.get("iem_cd", "")).strip().lstrip("A")[-6:]
            holdings[code] = Holding(code, (r.get("iem_nm") or "").strip(), qty,
                                     _int(r.get("phs_pr")), _int(r.get("now_pr")))
        cash = _int(o0.get("orr_pbl_amt4")) or _int(o0.get("nxt2_dd_dca")) or _int(o0.get("dca"))
        return Balance(orderable_cash=cash, holdings=holdings)

    def buyable_qty(self, code: str, price: int) -> int:
        data = self._post("/krstock/inquiry/v1/buyableQuantity", {
            "ost_dit_cd": "1", "act_no": self.account, "iem_cd": code,
            "nmn_pr_tp_cd": "01", "orr_pr": price,
        })
        return _int((data.get("Output_0") or {}).get("csh_orr_pbl_qty"))

    def sellable_qty(self, code: str) -> int:
        data = self._post("/krstock/inquiry/v1/sellableQuantity", {
            "act_no": self.account, "iem_cd": code, "cfd_lon_cd": "00",
        })
        return _int((data.get("Output_0") or {}).get("sll_pbl_qty"))

    # ── 주문 ────────────────────────────────────────────
    def _order(self, side: str, code: str, qty: int, price: int | None) -> OrderResult:
        if self.mode == "dry":
            return OrderResult(True, "DRY", f"[드라이런] {side} {code} {qty}주 (전송 안 함)")
        body = {
            "act_no": self.account, "iem_cd": code, "orr_qty": qty,
            "nmn_pr_tp_cd": "05" if price is None else "01",
            "orr_cnd_dit_cd": "00", "ssl_nmn_pr_dit_cd": "00",
            "rmt_mkt_cd": "KRX", "sor_mkt_sli_yn": "N",
        }
        if price is not None:
            body["orr_pr"] = price
        path = "/krstock/order/v1/cashBuy" if side == "BUY" else "/krstock/order/v1/cashSell"
        data = self._post(path, body)
        o = data.get("Output_0") or {}
        order_no = str(o.get("mkt_orr_no") or o.get("anw_cld_mkt_orr_no1") or "").strip()
        ok = bool(order_no) and order_no != "0"
        return OrderResult(ok, order_no, self._msg(data))

    def buy(self, code: str, qty: int, price: int | None = None) -> OrderResult:
        return self._order("BUY", code, qty, price)

    def sell(self, code: str, qty: int, price: int | None = None) -> OrderResult:
        return self._order("SELL", code, qty, price)
