"""토스증권 Open API 래퍼 — 국내(KR)·미국(US) 주식.

공식 명세: https://openapi.tossinvest.com/openapi-docs/latest/openapi.json (1.2.24 기준)
  - 인증: OAuth2 client_credentials (폼 인코딩). 클라이언트당 유효 토큰은 1개 —
    재발급하면 이전 토큰이 즉시 무효화되므로, 파일 캐시를 프로세스(타이머·웹) 간에 공유한다.
  - 계좌 API 는 X-Tossinvest-Account: accountSeq 헤더 필요
  - 허용 IP 등록 필수 (토스증권 PC웹 → 설정 → Open API). 미등록 IP 는 403
  - ⚠ 모의투자 환경이 없다 → TRADING_MODE 는 dry 또는 live 만 허용
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import requests

from .broker import KST, Balance, BrokerError, Holding, OrderResult, Quote, _num

log = logging.getLogger(__name__)
BASE = "https://openapi.tossinvest.com"

# 호출 그룹별 최소 간격(초) — 공개된 한도(ACCOUNT·STOCK_ALL 초당 1회, ORDER 10, MARKET_DATA 15, CHART 20)보다 보수적으로
INTERVAL = {"ACCOUNT": 1.1, "ASSET": 1.1, "ORDER_INFO": 1.1, "ORDER": 0.15, "STOCK": 0.2,
            "MARKET_DATA": 0.1, "MARKET_DATA_CHART": 0.08, "MARKET_INFO": 0.2}


def _group(method: str, path: str) -> str:
    if path.startswith("/api/v1/accounts"):
        return "ACCOUNT"
    if path.startswith("/api/v1/holdings"):
        return "ASSET"
    if path.startswith(("/api/v1/buying-power", "/api/v1/sellable-quantity", "/api/v1/commissions")):
        return "ORDER_INFO"
    if path.startswith("/api/v1/orders"):
        return "ORDER" if method == "POST" else "ORDER_INFO"
    if path.startswith("/api/v1/candles"):
        return "MARKET_DATA_CHART"
    if path.startswith("/api/v1/stocks"):
        return "STOCK"
    if path.startswith(("/api/v1/market-calendar", "/api/v1/exchange-rate")):
        return "MARKET_INFO"
    return "MARKET_DATA"


def _ts(v) -> datetime | None:
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


class TossBroker:
    def __init__(self, mode: str, market: str, token_dir: Path, account_seq: str | None = None):
        if mode == "mock":
            raise BrokerError("토스증권은 모의투자 환경이 없습니다. TRADING_MODE 를 dry(주문 없음) 또는 live 로 쓰세요.")
        if market not in ("kr", "us"):
            raise BrokerError(f"토스 시장 구분 오류: {market}")
        self.mode, self.market = mode, market
        self.env = "live"
        self.currency = "KRW" if market == "kr" else "USD"
        self.client_id = os.environ.get("TOSS_CLIENT_ID", "").strip()
        self.client_secret = os.environ.get("TOSS_CLIENT_SECRET", "").strip()
        if not (self.client_id and self.client_secret):
            raise BrokerError(".env 에 TOSS_CLIENT_ID / TOSS_CLIENT_SECRET 을 넣으세요 (토스증권 PC웹 → 설정 → Open API)")
        self.token_file = token_dir / "toss_token.json"
        self.session = requests.Session()
        self._token: str | None = None
        self._last: dict[str, float] = {}
        self._names: dict[str, str] = {}
        self._calendar: dict | None = None
        self.account_seq = account_seq or os.environ.get("TOSS_ACCOUNT_SEQ", "").strip() or self._pick_account()
        self.account = f"seq {self.account_seq}"

    # ── 인증 (프로세스 간 공유 캐시 + 잠금: 동시에 재발급하면 서로의 토큰을 무효화하므로) ──
    def _issue(self) -> str:
        with open(self.token_file.with_suffix(".lock"), "w") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            try:   # 잠금 대기 중 다른 프로세스가 새로 받았을 수 있다
                saved = json.loads(self.token_file.read_text())
                if saved.get("client_id") == self.client_id and saved["exp"] > time.time() + 300 \
                        and saved["token"] != self._token:
                    return saved["token"]
            except (OSError, ValueError, KeyError):
                pass
            r = self.session.post(f"{BASE}/oauth2/token", timeout=15, data={
                "grant_type": "client_credentials", "client_id": self.client_id,
                "client_secret": self.client_secret})
            body = r.json() if r.content else {}
            if r.status_code == 403:
                raise BrokerError("토스 API 접근 거부(403) — 이 서버의 공인 IP 가 허용 IP 로 등록됐는지 확인하세요")
            if r.status_code != 200 or not body.get("access_token"):
                raise BrokerError(f"토스 토큰 발급 실패 ({r.status_code}): "
                                  f"{body.get('error_description') or body.get('error') or r.text[:200]}")
            exp = time.time() + int(body.get("expires_in", 86400))
            self.token_file.write_text(json.dumps({"token": body["access_token"], "exp": exp,
                                                   "client_id": self.client_id}))
            os.chmod(self.token_file, 0o600)
            return body["access_token"]

    def _get_token(self, force: bool = False) -> str:
        if self._token and not force:
            return self._token
        if not force:
            try:
                saved = json.loads(self.token_file.read_text())
                if saved.get("client_id") == self.client_id and saved["exp"] > time.time() + 300:
                    self._token = saved["token"]
                    return self._token
            except (OSError, ValueError, KeyError):
                pass
        self._token = self._issue()
        return self._token

    # ── 공통 호출 ──
    def _req(self, method: str, path: str, params: dict | None = None, body: dict | None = None,
             account: bool = False) -> dict | list:
        group = _group(method, path)
        for attempt in range(3):
            wait = INTERVAL.get(group, 0.2) - (time.monotonic() - self._last.get(group, 0))
            if wait > 0:
                time.sleep(wait)
            headers = {"Authorization": f"Bearer {self._get_token(force=attempt > 0 and r.status_code == 401)}"}
            if account:
                headers["X-Tossinvest-Account"] = str(self.account_seq)
            r = self.session.request(method, BASE + path, params=params, json=body, headers=headers, timeout=20)
            self._last[group] = time.monotonic()
            if r.status_code == 401 and attempt == 0:
                continue                                   # 토큰 무효 → 재발급 후 1회 재시도
            if r.status_code == 429 and attempt < 2:
                ra = r.headers.get("Retry-After", "1")
                time.sleep(min(float(ra) if ra.replace(".", "").isdigit() else 1.0, 5.0))
                continue
            break
        try:
            data = r.json()
        except ValueError:
            raise BrokerError(f"토스 {path} HTTP {r.status_code}: {r.text[:200]}") from None
        if r.status_code == 403:
            raise BrokerError("토스 API 접근 거부(403) — 허용 IP 미등록이거나 권한이 없습니다: "
                              + str((data.get("error") or {}).get("message", "")))
        if r.status_code >= 400 or "error" in data:
            err = data.get("error") or {}
            raise BrokerError(f"토스 {path} 실패 [{err.get('code', r.status_code)}] {err.get('message', '')}")
        return data.get("result")

    def _pick_account(self) -> str:
        rows = self._req("GET", "/api/v1/accounts") or []
        seqs = [str(a["accountSeq"]) for a in rows if a.get("accountType", "BROKERAGE") == "BROKERAGE"]
        if len(seqs) == 1:
            return seqs[0]
        if not seqs:
            raise BrokerError("토스 종합매매 계좌가 없습니다")
        raise BrokerError(f"토스 계좌가 여러 개입니다: {seqs} → .env 에 TOSS_ACCOUNT_SEQ 지정")

    def list_accounts(self) -> list[dict]:
        return [{"acct_no": a.get("accountNo"), "acct_type": a.get("accountType"),
                 "stock_ok": a.get("accountType") == "BROKERAGE", "seq": a.get("accountSeq")}
                for a in (self._req("GET", "/api/v1/accounts") or [])]

    # ── 장 운영 ──
    def _market_open(self) -> bool:
        if self._calendar is None:
            self._calendar = self._req("GET", f"/api/v1/market-calendar/{self.market.upper()}") or {}
        now = datetime.now(KST)
        for day in ("today", "previousBusinessDay"):
            d = self._calendar.get(day) or {}
            sess = (d.get("integrated") or {}).get("regularMarket") if self.market == "kr" else d.get("regularMarket")
            if sess:
                a, b = _ts(sess.get("startTime")), _ts(sess.get("endTime"))
                if a and b and a <= now <= b:
                    return True
        return False

    # ── 시세 ──
    def _name(self, code: str) -> str:
        if code not in self._names:
            try:
                for s in self._req("GET", "/api/v1/stocks", {"symbols": code}) or []:
                    self._names[s["symbol"]] = s.get("name") or code
            except BrokerError:
                pass
        return self._names.get(code, code)

    def _warnings(self, code: str) -> list[str]:
        if self.market != "kr":
            return []
        labels = {"LIQUIDATION_TRADING": "정리매매", "OVERHEATED": "단기과열",
                  "INVESTMENT_WARNING": "투자경고", "INVESTMENT_RISK": "투자위험"}
        today = datetime.now(KST).strftime("%Y-%m-%d")
        out = []
        try:
            for w in self._req("GET", f"/api/v1/stocks/{code}/warnings") or []:
                t = w.get("warningType")
                if t in labels and (w.get("startDate") or "") <= today <= (w.get("endDate") or "9999"):
                    out.append(labels[t])
        except BrokerError as e:
            log.info("warnings %s: %s", code, e)
        return out

    def daily(self, code: str, n: int = 80) -> list[dict]:
        res = self._req("GET", "/api/v1/candles", {"symbol": code, "interval": "1d",
                                                    "count": min(n, 200), "adjusted": "true"}) or {}
        bars = [{"date": str(c.get("timestamp", ""))[:10].replace("-", ""),
                 "open": _num(c.get("openPrice")), "high": _num(c.get("highPrice")),
                 "low": _num(c.get("lowPrice")), "close": _num(c.get("closePrice")),
                 "volume": _num(c.get("volume"))} for c in res.get("candles") or [] if _num(c.get("closePrice")) > 0]
        bars.sort(key=lambda b: b["date"])          # 토스는 최신순 → 오래된 순으로
        return bars

    def quote(self, code: str) -> Quote:
        rows = self._req("GET", "/api/v1/prices", {"symbols": code}) or []
        if not rows or not _num(rows[0].get("lastPrice")):
            raise BrokerError(f"{code} 현재가 없음")
        price = _num(rows[0]["lastPrice"])
        if self.market == "kr":
            price = int(price)
        bars = self.daily(code, 2)
        prev = bars[-2]["close"] if len(bars) >= 2 else 0
        return Quote(
            code=code, name=self._name(code), price=price,
            change_pct=round((price / prev - 1) * 100, 2) if prev else 0.0,
            ask=price, bid=price, market_status="1" if self._market_open() else "",
            market="", sector="", per=0.0, pbr=0.0, foreign_pct=0.0, w52_high=0, w52_low=0,
            warnings=self._warnings(code),
        )

    # ── 계좌 ──
    def balance(self) -> Balance:
        res = self._req("GET", "/api/v1/holdings", account=True) or {}
        want = self.market.upper()
        holdings = {}
        for it in res.get("items") or []:
            if (it.get("marketCountry") or want) != want:
                continue
            qty = _num(it.get("quantity"))
            if qty <= 0:
                continue
            conv = int if self.market == "kr" else (lambda x: round(x, 4))
            code = it.get("symbol")
            holdings[code] = Holding(code, it.get("name") or code,
                                     int(qty) if qty == int(qty) else qty,
                                     conv(_num(it.get("averagePurchasePrice"))), conv(_num(it.get("lastPrice"))))
        bp = self._req("GET", "/api/v1/buying-power", {"currency": self.currency}, account=True) or {}
        cash = _num(bp.get("cashBuyingPower"))
        return Balance(orderable_cash=int(cash) if self.market == "kr" else round(cash, 2), holdings=holdings)

    def buyable_qty(self, code: str, price: float) -> int:
        bp = self._req("GET", "/api/v1/buying-power", {"currency": self.currency}, account=True) or {}
        cash = _num(bp.get("cashBuyingPower"))
        return int(cash // price) if price else 0

    def sellable_qty(self, code: str) -> int:
        res = self._req("GET", "/api/v1/sellable-quantity", {"symbol": code}, account=True) or {}
        return int(_num(res.get("sellableQuantity")))

    # ── 주문 ──
    def _order(self, side: str, code: str, qty: int, price: float | None) -> OrderResult:
        if self.mode == "dry":
            return OrderResult(True, "DRY", f"[드라이런] {side} {code} {qty}주 (전송 안 함)")
        body = {"clientOrderId": f"ai-trader-{uuid.uuid4().hex[:20]}", "symbol": code, "side": side,
                "orderType": "MARKET" if price is None else "LIMIT", "quantity": str(int(qty))}
        if price is not None:
            body["price"] = str(int(price)) if self.market == "kr" else f"{price:.2f}"
        try:
            res = self._req("POST", "/api/v1/orders", body=body, account=True) or {}
        except BrokerError as e:
            return OrderResult(False, "", str(e))
        oid = str(res.get("orderId") or "")
        return OrderResult(bool(oid), oid[:16], "주문 접수")

    def buy(self, code: str, qty: int, price=None) -> OrderResult:
        return self._order("BUY", code, qty, price)

    def sell(self, code: str, qty: int, price=None) -> OrderResult:
        return self._order("SELL", code, qty, price)
