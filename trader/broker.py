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


@dataclass
class OrderFill:
    """주문 체결 상태. state: open(대기) · partial(일부 체결, 대기 중) · filled · canceled · rejected"""
    filled: float          # 체결 수량 (누적)
    avg_price: float       # 평균 체결가 (체결 없으면 0)
    open_qty: float        # 아직 대기 중인 수량
    state: str
    fee: float | None = None   # 증권사가 알려 주면 실제 수수료·세금
    tax: float | None = None

    @property
    def done(self) -> bool:
        return self.state in ("filled", "canceled", "rejected")


class BrokerError(RuntimeError):
    pass


class AccountChoice(BrokerError):
    """국내주식 계좌 후보가 여러 개 — 사용자가 하나를 골라야 한다 (웹은 고르는 칸을 보여 준다)."""

    def __init__(self, msg: str, env: str, candidates: list[str]):
        super().__init__(msg)
        self.env, self.candidates = env, candidates


class NHBroker:
    """mode: dry | mock | live. dry 는 조회만 하고 주문은 절대 내지 않는다."""

    # 모의투자 서버는 시세 API 를 제공하지 않는다(IGW40023) → 시세만 실전 서버에서 조회.
    # 시세는 계좌와 무관한 공개 데이터라 안전하며, 주문·잔고는 절대 실전으로 가지 않는다.
    QUOTE_PREFIX = ("/krstock/quote/", "/gbstock/quote/")
    _url_lock = threading.Lock()

    URLS = {"mock": "https://moapi.nhplug.com:8443", "live": "https://api.nhplug.com:8443"}

    def __init__(self, mode: str, account: str | None = None):
        from nhplug import call, get_base_url, status_of  # 지연 import (테스트에서 불필요)

        self._call, self._status_of = call, status_of
        self.mode = mode
        env_base = get_base_url()

        def env_of(url: str) -> str:
            return "mock" if url.split("//")[-1].split("/")[0].lower().startswith("moapi.") else "live"

        # 서버는 모드로 정한다 (웹에서 모의↔실계좌를 바꿔도 .env 를 고칠 필요 없게).
        # dry 는 .env 의 NHPLUG_BASE_URL 을 따른다. .env 주소가 같은 환경이면 그 주소(포트 등)를 존중.
        if mode in self.URLS:
            self.env = mode
            base = env_base if env_of(env_base) == mode else self.URLS[mode]
        else:
            self.env, base = env_of(env_base), env_base
        self._base = base
        self._quote_base = base.replace("://moapi.", "://api.", 1) if self.env == "mock" else base

        self.account = (account or os.environ.get(f"NHPLUG_ACCOUNT_{self.env.upper()}")
                        or self._checked(os.environ.get("NHPLUG_ACCOUNT")) or self._pick_account())

    def _checked(self, acct: str | None) -> str | None:
        """NHPLUG_ACCOUNT 가 지금 환경(모의/실계좌)의 계좌가 아니면 무시하고 자동 선택한다.
        (모의투자 때 넣어 둔 계좌번호로 실계좌 주문이 나가려다 실패하는 일 방지)"""
        if not acct or self.mode == "dry":
            return acct
        try:
            rows = self._post("/n2/acctinfo", {}).get("Output_0")
        except Exception:  # noqa: BLE001
            return acct
        if not isinstance(rows, list) or not rows:
            return acct
        want = {"03"} if self.env == "mock" else {"01", "02"}
        mine = {str(r.get("acct_no", "")).strip() for r in rows if (r.get("acct_type") or "").strip() in want}
        return acct if acct in mine else None

    # ── 공통 ────────────────────────────────────────────
    def _post(self, path: str, body: dict) -> dict:
        # nhplug 는 환경변수로 서버를 고른다 → 호출마다 잠금 안에서 이번 요청의 서버로 맞췄다가 되돌린다.
        # 모의투자 서버는 시세를 주지 않아 시세만 실전 서버로 보낸다(공개 데이터). 주문·잔고는 절대 섞이지 않는다.
        target = self._quote_base if path.startswith(self.QUOTE_PREFIX) else self._base
        with self._url_lock:
            prev = os.environ.get("NHPLUG_BASE_URL")
            os.environ["NHPLUG_BASE_URL"] = target
            try:
                data = self._call(path, body)
            finally:
                if prev is None:
                    os.environ.pop("NHPLUG_BASE_URL", None)
                else:
                    os.environ["NHPLUG_BASE_URL"] = prev
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
        raise AccountChoice(f"국내주식 계좌 후보가 여러 개입니다: {cand} → 웹 설정 → 실행 모드에서 고르거나 "
                            f".env 에 NHPLUG_ACCOUNT_{self.env.upper()}=<계좌번호> "
                            "(나무 앱의 '국내주식' 계좌번호와 같은 것)", self.env, cand)

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

    def scan_quote(self, code: str) -> dict:
        """자동 종목 검색용 가벼운 조회 — 현재가 API 1회 (장상태 판단·일봉 없음)."""
        o = self._post("/krstock/quote/v1/currentPrice", {"iem_cd": code, "market_cd": "KRX"}).get("Output_0") or {}
        warns = []
        if (o.get("invt_epmd_yn") or "").strip() == "Y" or (o.get("sltr_yn") or "").strip() == "Y":
            warns.append("유의/정리")
        if (o.get("mrkt_alrm_code") or "").strip() in {"1", "2", "3", "4"}:
            warns.append("경고")
        if (o.get("short_over_code") or "").strip() in {"1", "2", "3"}:
            warns.append("단기과열")
        return {"price": _int(o.get("stck_prpr")) or _int(o.get("stck_prdy_clpr")),
                "prev_value": _int(o.get("prdy_tr_pbmn")), "warnings": warns}

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

    # ── 체결 확인 · 취소 ─────────────────────────────────
    def order_status(self, order_no: str, code: str, side: str, qty: int, price: float, day: str) -> OrderFill | None:
        """day: 주문일 YYYY-MM-DD. 주문을 못 찾으면 None."""
        data = self._post("/krstock/inquiry/v1/dailyOrderExecution", {
            "orr_dt": day.replace("-", ""), "act_no": self.account, "orr_mkt_cd": "00", "ost_cns_dit": "0"})
        rows = data.get("Output_0") or []
        if isinstance(rows, dict):
            rows = [rows]
        want = "매수" if side == "BUY" else "매도"

        def same(r, by_no: bool) -> bool:
            if str(r.get("iem_cd", "")).strip().lstrip("A")[-6:] != code:
                return False
            if by_no:
                return str(_int(r.get("itg_orr_no"))) == str(_int(order_no))
            return (want in str(r.get("sby_dit_cd_nm", "")) and _int(r.get("orr_qty")) == int(qty)
                    and (not price or abs(_num(r.get("orr_pr")) - float(price)) < 0.5))
        r = next((x for x in rows if same(x, True)), None) or next((x for x in rows if same(x, False)), None)
        if r is None:
            return None
        filled, open_q, can = _int(r.get("tot_cns_qty")), _int(r.get("ny_cns_qty")), _int(r.get("can_qty"))
        rej = str(r.get("orr_rjt_rsn_cd_nm") or "").strip()
        state = ("rejected" if rej and not filled else "filled" if filled >= int(qty)
                 else "canceled" if can or (open_q == 0 and filled < int(qty)) else "partial" if filled else "open")
        return OrderFill(filled, _num(r.get("cns_avg_uit_pr")), open_q, state)

    def cancel(self, order_no: str, code: str) -> OrderResult:
        if self.mode == "dry":
            return OrderResult(True, "DRY", "[드라이런] 취소")
        data = self._post("/krstock/order/v1/cancel", {
            "act_no": self.account, "org_mkt_orr_no": _int(order_no), "all_pat_dit_cd": "1", "iem_cd": code})
        o = data.get("Output_0") or {}
        no = str(o.get("mkt_orr_no") or o.get("can_cld_mkt_orr_no1") or "").strip()
        return OrderResult(bool(no) and no != "0", no, self._msg(data))
