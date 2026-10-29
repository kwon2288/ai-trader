"""NH투자증권(나무) 해외주식 — 미국 주식 래퍼.

필드명은 공식 명세(gbstock openapi.json, 명세 261015) 기준.
  - 종목코드 = 티커(AAPL), 국가코드 200(미국)
  - 증거금 통화: 원화(2)로 주문하면 달러 환전 없이 원화로 바로 매수 (config [us] margin_currency)
  - 모의투자·운영 모두 제공. 시세는 국내와 같은 방식으로 실전 서버에서 조회한다(공개 데이터).
"""
from __future__ import annotations

import os
from datetime import datetime, time
from zoneinfo import ZoneInfo

from .broker import Balance, BrokerError, Holding, NHBroker, OrderResult, Quote, _num

ET = ZoneInfo("America/New_York")
NATION_US = "200"


def _f(v) -> float:
    return round(_num(v), 4)


def us_regular_hours(now: datetime | None = None) -> bool:
    n = (now or datetime.now(ET)).astimezone(ET)
    return n.weekday() < 5 and time(9, 30) <= n.time() <= time(16, 0)


class NHUSBroker(NHBroker):
    market = "us"

    def __init__(self, mode: str, account: str | None = None, margin_currency: str = "krw"):
        super().__init__(mode, account or os.environ.get("NHPLUG_ACCOUNT_US") or None)
        self.wtm = "2" if margin_currency.lower() == "krw" else "1"   # 2.원화 증거금 / 1.달러
        self.fx = 0.0                                                # 최근 조회 환율 (원/달러)

    # ── 시세 ──
    def quote(self, code: str) -> Quote:
        o = self._post("/gbstock/quote/v1/current", {"iem_cd": code}).get("Output_0") or {}
        price = _f(o.get("trdprc")) or _f(o.get("hst_trdprc"))
        if not price:
            raise BrokerError(f"{code} 현재가 조회 실패")
        if _num(o.get("currency_prc")):
            self.fx = _num(o.get("currency_prc"))
        period = (o.get("marketperiod_cls") or "").strip()
        if period:
            status = "1" if period == "1" else period
        else:
            status = "1" if us_regular_hours() else ""
        return Quote(
            code=code, name=(o.get("iem_nm") or code).strip(), price=price,
            change_pct=_num(o.get("pctchng")),
            ask=_f(o.get("best_ask1")) or price, bid=_f(o.get("best_bid1")) or price,
            market_status=status, market=(o.get("exch_name") or "").strip(),
            sector=(o.get("industry_name") or "").strip(),
            per=_num(o.get("per_prc")), pbr=0.0, foreign_pct=0.0,
            w52_high=_f(o.get("w52high_prc")), w52_low=_f(o.get("w52low_prc")), warnings=[],
        )

    def daily(self, code: str, n: int = 80) -> list[dict]:
        data = self._post("/gbstock/quote/v1/period", {
            "iem_cd": code, "end_dt": datetime.now(ET).strftime("%Y%m%d"), "count": str(n),
            "maxavg": "0", "gubun": "3", "xtick": "0001", "today_cls": "1", "market_cls": "1",
        })
        rows = data.get("Output_1") or []
        bars = [{"date": str(r.get("trade_date") or r.get("bsop_date") or ""),
                 "open": _f(r.get("open_prc")), "high": _f(r.get("high")), "low": _f(r.get("low")),
                 "close": _f(r.get("close_prc")), "volume": _num(r.get("movolume"))}
                for r in rows if _num(r.get("close_prc")) > 0]
        if not bars:   # 일부 응답은 Output_0 에 일별 행이 온다
            bars = [{"date": str(r.get("date") or ""), "open": _f(r.get("open_prc")), "high": _f(r.get("high")),
                     "low": _f(r.get("low")), "close": _f(r.get("trdprc")), "volume": _num(r.get("acvol"))}
                    for r in (data.get("Output_0") or []) if _num(r.get("trdprc")) > 0]
        bars.sort(key=lambda b: b["date"])
        return bars[-n:]

    # ── 계좌 ──
    def _ensure_fx(self) -> float:
        """원화 증거금 금액을 달러로 바꾸려면 환율이 필요하다 → 아직 모르면 대표 종목 시세로 한 번 받아온다."""
        if not self.fx:
            try:
                o = self._post("/gbstock/quote/v1/current", {"iem_cd": "AAPL"}).get("Output_0") or {}
                self.fx = _num(o.get("currency_prc"))
            except Exception:  # noqa: BLE001
                pass
        return self.fx

    def _buyable(self, pcs: str, code: str = "", price: float = 0) -> dict:
        return self._post("/gbstock/inquiry/v1/buyableAmount", {
            "act_no": self.account, "pcs_dit": pcs, "fc_sec_trd_nat_cd": NATION_US, "iem_cd": code,
            "fc_orr_uit_pr": round(price, 2), "wtm_cur_knd_cd": self.wtm, "oss_orr_knd_cd": "1",
            "ahi_nmn_pr_tp_cd": "00", "cfd_lon_cd": "00",
        }).get("Output_0") or {}

    def balance(self) -> Balance:
        data = self._post("/gbstock/inquiry/v1/balance", {
            "act_no": self.account, "qut_iqr_dit_cd": "1", "fc_sec_trd_nat_cd": NATION_US,
            "cur_cd": "USD", "xns_dit_cd": "0",
        })
        holdings = {}
        for r in data.get("Output_1") or []:
            qty = int(_num(r.get("cns_bse_bnc_qty")))
            if qty <= 0:
                continue
            code = (r.get("iem_cd") or "").strip().upper()
            holdings[code] = Holding(code, (r.get("iem_nm") or code).strip(), qty,
                                     _f(r.get("fc_avg_phs_pr") or r.get("fc_phs_uit_pr")),
                                     _f(r.get("fc_sec_end_pr")))
        # 주문 가능 금액(달러 환산). 원화 증거금이면 원화 금액을 환율로 나눈다.
        cash = 0.0
        try:
            o = self._buyable("1")
            amt = _num(o.get("orr_pbl_amt"))
            if (o.get("wtm_cur_cd") or "USD").strip().upper() == "KRW":
                cash = amt / self._ensure_fx() if self._ensure_fx() else 0.0
            else:
                cash = amt
        except Exception:  # noqa: BLE001
            o0 = data.get("Output_0") or {}
            cash = _num(o0.get("fc_dca"))
        if cash <= 0 and self.wtm == "2" and not self.fx:
            cash = float("inf")   # 환율을 아직 모르면 봇 장부 예산을 상한으로 쓴다 (주문 직전 수량을 다시 확인)
        return Balance(orderable_cash=round(cash, 2) if cash != float("inf") else cash, holdings=holdings)

    def buyable_qty(self, code: str, price: float) -> int:
        o = self._buyable("2", code, price)
        return int(_num(o.get("orr_pbl_qty") or o.get("byn_pbl_qty") or o.get("max_pbl_qty")))

    def sellable_qty(self, code: str) -> int:
        o = self._buyable("3", code)
        return int(_num(o.get("sll_pbl_qty") or o.get("sll_pbl_qty1")))

    def scan_quote(self, code: str):   # 미국 자동 선정은 아직 미지원
        raise BrokerError("미국 자동 종목 선정은 아직 지원하지 않습니다")

    # ── 주문 ──
    def _order(self, side: str, code: str, qty: int, price: float | None) -> OrderResult:
        if self.mode == "dry":
            return OrderResult(True, "DRY", f"[드라이런] {side} {code} {qty}주 (전송 안 함)")
        body = {
            "act_no": self.account, "fc_sec_trd_nat_cd": NATION_US, "iem_cd": code, "orr_qty": int(qty),
            "ahi_nmn_pr_tp_cd": "03" if price is None else "00", "wtm_cur_knd_cd": self.wtm,
        }
        if price is not None:
            body["fc_orr_uit_pr"] = round(price, 2)
        path = "/gbstock/order/v1/buy" if side == "BUY" else "/gbstock/order/v1/sell"
        data = self._post(path, body)
        no = str((data.get("Output_0") or {}).get("orr_no") or "").strip()
        return OrderResult(bool(no) and no != "0", no, self._msg(data))
