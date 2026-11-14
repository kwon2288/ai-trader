"""한 번 실행 = 한 번의 '분석 → 위험관리 → 주문' 사이클. 스케줄러가 하루 1~2회 호출한다."""
from __future__ import annotations

import time

import logging
import math
from datetime import timedelta

from . import indicators
from .broker import Holding
from .config import Config
from .journal import Journal, now

log = logging.getLogger(__name__)


def own_holdings(j: Journal, cfg: Config, holdings: dict, say=None) -> dict:
    """계좌 보유 중 봇 몫만. 같은 종목을 사용자가 따로 가졌으면 봇이 산 수량·평단만 본다."""
    bot_codes = j.bot_codes(cfg.market)
    lots = j.bot_lots(cfg.market, cfg.ledger)
    held = {}
    for c, h in holdings.items():
        if c not in bot_codes:
            continue
        bq, ba = lots.get(c, (None, None))
        if bq is not None:
            if h.qty > bq and say:
                say(f"🔒 {h.name}: 계좌 {h.qty}주 중 봇 몫 {bq}주만 관리 (나머지 {h.qty - bq}주는 사용자 보유)")
            h = Holding(c, h.name, min(h.qty, bq), ba or h.avg_price, h.now_price)
        held[c] = h
    return held


def account_view(bal, held: dict) -> dict:
    """웹 ‘내 계좌 전체’ 용 — 봇 몫과 사용자 보유를 모두 보여 준다 (조회만, 매매와 무관)."""
    rows, value, cost = [], 0.0, 0.0
    for c, h in sorted(bal.holdings.items(), key=lambda kv: -kv[1].qty * kv[1].now_price):
        v, k = h.qty * h.now_price, h.qty * h.avg_price
        value, cost = value + v, cost + k
        rows.append({"code": c, "name": h.name, "qty": h.qty, "avg_price": h.avg_price, "now_price": h.now_price,
                     "value": round(v, 2), "pnl": round(v - k, 2), "pnl_pct": round(h.pnl_pct, 2),
                     "bot_qty": held[c].qty if c in held else 0})
    cash = bal.orderable_cash if bal.orderable_cash != float("inf") else None
    return {"holdings": rows, "value": round(value, 2), "cost": round(cost, 2),
            "pnl": round(value - cost, 2), "pnl_pct": round((value / cost - 1) * 100, 2) if cost else 0.0,
            "cash": cash}


CLOSE = {"kr": ("Asia/Seoul", 15, 30), "us": ("America/New_York", 16, 0)}
CLOSE_WINDOW_MIN = 30          # 장중 감시 간격(최대 30분)과 맞춤


def near_close(market: str, at=None) -> int:
    """정규장 마감까지 남은 분 (마감 30분 전 ~ 마감 사이일 때만, 아니면 0)."""
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo
    tz, hh, mm = CLOSE.get(market, CLOSE["kr"])
    t = (at or _dt.now(ZoneInfo(tz))).astimezone(ZoneInfo(tz))
    if t.weekday() >= 5:
        return 0
    left = (hh * 60 + mm) - (t.hour * 60 + t.minute)
    return left if 0 < left <= CLOSE_WINDOW_MIN else 0


def last_slot_wait(cfg, at=None) -> str | None:
    """오늘 마지막 정기 실행 시각이 아직 안 됐으면 그 시각 'HH:MM'(그 시장 시간), 됐으면 None.
    정기 실행 시각을 모르면 None (제한하지 않음)."""
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo
    from . import schedule
    try:
        import tomllib
        from .config import ensure_config
        raw = tomllib.loads(ensure_config().read_text(encoding="utf-8"))
        slots = schedule.as_tuples(schedule.times(raw, cfg.market))
    except Exception:  # noqa: BLE001
        return None
    if not slots:
        return None
    hh, mm = max(slots)
    t = (at or _dt.now(ZoneInfo(schedule.TZ[cfg.market]))).astimezone(ZoneInfo(schedule.TZ[cfg.market]))
    return f"{hh:02d}:{mm:02d}" if t.hour * 60 + t.minute < hh * 60 + mm - 5 else None


def account_label(scope: str | None) -> str:
    """'nh/live' → '나무 실계좌'."""
    if not scope:
        return "?"
    b, _, m = scope.partition("/")
    return " ".join(x for x in ({"nh": "나무", "kis": "한국투자", "toss": "토스"}.get(b, b),
                                {"live": "실계좌", "mock": "모의투자", "dry": "드라이런"}.get(m, m)) if x)


def strategy_mode(cfg) -> str:
    from . import strategy
    return strategy.settings(cfg)["signal"]



def _indent(text: str, first: str, rest: str | None = None) -> str:
    """AI 여럿의 의견(여러 줄)을 로그·텔레그램에서 한 사람당 한 줄로 들여 쓴다."""
    from .analyst import ai_lines
    lines = [f"{w}: {b}" if w else b for w, b in ai_lines(text)] or [""]
    rest = first if rest is None else rest
    return "\n".join((first if i == 0 else rest) + ln for i, ln in enumerate(lines))

class Bot:
    def __init__(self, cfg: Config, broker, analyst, journal: Journal | None = None):
        self.cfg, self.broker, self.analyst = cfg, broker, analyst
        self.j = journal or Journal(cfg.db_path)
        from .runner import broker_name
        self.j.use_broker(broker_name(cfg), cfg.market, cfg.mode)
        try:                                              # 설정 버전 — 이 실행의 주문에 붙여 둠 (설정별 손익)
            from . import setver
            self.j.setver[cfg.ledger] = setver.current(self.j, cfg)
        except Exception as e:  # noqa: BLE001
            log.warning("설정 버전 기록 실패: %s", e)
        self.lines: list[str] = []
        self.orders_left = 0
        self.sent = 0          # 이번 실행에서 보낸(체결 확인할) 주문 수
        self.no_buy_reason = None
        self.paused: set[str] = set()
        self.acct_qty: dict[str, int] = {}     # 이번 실행 시작 때 계좌 수량 (응답 없는 주문 확인용)
        self.stages: list[tuple[str, float]] = []    # 단계별 걸린 시간 (실행 로그 끝에 표시)
        self._mark = None

    def stage(self, name: str) -> None:
        """직전 표시 이후 걸린 시간을 name 단계로 기록."""
        t = time.monotonic()
        if self._mark is not None:
            self.stages.append((name, t - self._mark))
        self._mark = t

    def say(self, s: str) -> None:
        log.info(s)
        self.lines.append(s)
        try:
            from . import runstatus
            runstatus.step(self.cfg.data_dir, s)
        except Exception:  # noqa: BLE001
            pass

    def _mk(self) -> str:
        return {"kr": "🇰🇷", "us": "🇺🇸"}.get(self.cfg.market, "") + ("" if self.cfg.mode == "live" else
                                                                       f"[{ {'mock': '모의', 'dry': '드라이런'}[self.cfg.mode] }]")

    def _usd_short_hint(self, st: dict, quotes: dict) -> None:
        """토스 미국: Open API 주문은 자동환전이 안 된다 → 달러가 모자라면 원화 잔액과 함께 환전하라고 알려 준다.
        로그에는 매번, 알림은 한 주도 살 수 없을 때 하루 한 번."""
        cfg = self.cfg
        if cfg.market != "us" or cfg.mode == "dry" or not hasattr(self.broker, "krw_cash"):
            return
        cash, room = st.get("account_cash"), st.get("room") or 0
        if cash is None or room <= 0 or cash >= room:
            return
        try:
            krw = float(self.broker.krw_cash())
        except Exception:  # noqa: BLE001
            krw = 0.0
        has = f"원화 {krw:,.0f}원은 있지만 " if krw > 0 else ""
        self.say(f"💱 토스 달러 주문가능 {cfg.money(cash)} < 남은 예산 {cfg.money(room)} — {has}"
                 f"토스 Open API 주문은 자동환전이 안 돼요 → 토스 앱에서 달러로 환전해 두세요")
        prices = [q.price for q in quotes.values() if getattr(q, "price", 0)]
        if not prices or cash >= min(prices):
            return                                     # 한 주라도 살 수 있으면 로그만
        from .fills import market_today
        key = f"usd_short_{market_today('us')}"
        if self.j.get(key):
            return
        self.j.set(key, "1")
        self.notify("errors", f"💱 {self._mk()} 달러가 모자라 매수할 수 없어요 — 주문가능 {cfg.money(cash)}"
                              f" (가장 싼 후보 {cfg.money(min(prices))})\n{has}"
                              f"토스 Open API 는 자동환전을 지원하지 않아요. 토스 앱에서 달러로 환전해 주세요.")

    def notify(self, kind: str, text: str) -> None:
        from . import notify
        notify.send(self.cfg, kind, text)

    # ── 예산: 고정 한도 (trader/ledger.py) ─────────────────
    # 지금 살 수 있는 금액 = min(예산 - 봇 투자 중, 계좌 주문가능 현금). 매수할 때마다 줄인다.
    avail: float = 0.0

    # ── 주문 실행 (공통) ────────────────────────────────
    def _exec(self, side: str, code: str, name: str, qty: int, est_price: int, trigger: str,
              force_market: bool = False, ignore_cap: bool = False) -> bool:
        if self.orders_left <= 0 and not ignore_cap:
            self.say(f"⛔ {name} {side} 건너뜀 — 오늘 주문 한도 소진")
            return False
        price = None
        if self.cfg.order_type == "limit" and not force_market:
            price = est_price
        fn = self.broker.buy if side == "BUY" else self.broker.sell
        from . import fills
        try:
            res = fn(code, qty, price)
        except Exception as e:  # noqa: BLE001
            # 응답 없음(시간 초과·네트워크) — 증권사엔 접수됐을 수 있다. 다시 보내지 않고 다음 확인 때 계좌 수량으로 판단.
            side_k = "매수" if side == "BUY" else "매도"
            if self.cfg.mode != "dry":
                fills.mark_unsure(self.j, self.cfg, side, code, name, qty, est_price, trigger,
                                  self.acct_qty.get(code, 0), str(e))
            self.paused.add(code)
            msg = (f"⚠ {name} {side_k} {qty}주 — 증권사 응답 없음 ({e}). 다시 보내지 않고, "
                   f"다음 실행 때 계좌 수량으로 체결 여부를 확인할게요")
            self.say(msg)
            self.notify("orders", f"⚠ {self._mk()} {msg}")
            return False
        track = res.ok and self.cfg.mode != "dry" and fills.supported(self.broker)
        self.j.add_order(self.cfg.ledger, side, code, name, qty, est_price, res, trigger,
                         status=None if (track or not res.ok or self.cfg.mode == "dry") else "filled")
        self.sent += int(bool(track))
        side_k = "매수" if side == "BUY" else "매도"
        if not res.ok:
            self.say(f"❌ {name} {side} {qty}주 실패: {res.message}")
            self.notify("orders", f"❌ {self._mk()} {side_k} 실패 — {name}({code}) {qty}주 [{trigger}]\n{res.message}")
            return False
        self.orders_left -= 1
        amt = round(qty * est_price, 2)
        self.say(f"✅ {side_k} {name}({code}) {qty}주 ≈{self.cfg.money(amt)} [{trigger}] "
                 f"주문번호 {res.order_no}")
        extra = ""
        if side == "SELL":
            lot = self.j.bot_lots(self.cfg.market, self.cfg.ledger).get(code, (None, None))
            if lot[1]:
                extra = f"\n손익 약 {self.cfg.money((est_price - lot[1]) * qty)} ({(est_price / lot[1] - 1) * 100:+.1f}%)"
        self.notify("orders", f"{'🟢' if side == 'BUY' else '🔵'} {self._mk()} {side_k} — {name}({code}) {qty}주 "
                              f"≈{self.cfg.money(amt)} [{trigger}]{extra}")
        if side == "BUY":
            self.avail -= amt
        if self.cfg.mode != "dry" and not track:      # 체결 조회가 안 되는 증권사: 바로 체결된 것으로
            if side == "BUY":
                self.j.open_position(code, name, self.cfg.market, qty, est_price)
            else:
                self.j.reduce_position(code, qty)
        return True

    def _direct_rule_sells(self, sig: dict, held: dict, quotes: dict, run_id, analyze_only: bool) -> int | None:
        """규칙 매도 신호를 AI 확인 없이 바로 실행 (sig 에서 뺀다). 매도 신호가 없으면 None, 있으면 실행 건수."""
        cfg = self.cfg
        sells = [c for c, s in sig.items() if s["type"].startswith("SELL") and c in held]
        if not sells:
            return None
        from . import signals
        n = 0
        for c in sells:
            s = sig.pop(c)
            h = held[c]
            d = {"code": c, "action": "SELL", "confidence": s["strength"], "sell_ratio": 1.0,
                 "reason": f"[규칙 매도 · {signals.label(strategy_mode(cfg))} — AI 확인 없이] {s['reason']}"}
            if self.j.held_days(c) < cfg.min_hold_days and not s.get("urgent"):      # 돌파 실패는 바로 정리
                outcome = f"보류 (최소 보유 {cfg.min_hold_days}일 미달)"
            elif analyze_only:
                outcome = "분석 전용 — 주문 안 함"
            else:
                qty = h.qty if cfg.mode == "dry" else min(h.qty, self.broker.sellable_qty(c))
                if qty <= 0:
                    outcome = "매도 가능 수량 없음"
                else:
                    q = quotes.get(c)
                    ok = self._exec("SELL", c, h.name, qty, (q.bid or q.price) if q else h.now_price,
                                    f"규칙 매도: {s['reason']}")
                    outcome = "매도 실행" if ok else "매도 실패"
                    if ok:
                        n += 1
                        if qty >= h.qty:
                            held.pop(c)
            self.j.add_decision(run_id, d, outcome, h.name)
            self.say(f"📏 규칙 매도 {h.name} → {outcome}\n    └ {s['reason']} (AI 확인 없이 바로)")
        return n

    def _daily_loss_check(self, st: dict) -> None:
        cfg = self.cfg
        if cfg.market == "us":
            from zoneinfo import ZoneInfo
            day = now().astimezone(ZoneInfo("America/New_York")).date().isoformat()
        else:
            day = now().date().isoformat()
        key = f"day_start_{cfg.ledger}"
        saved = (self.j.get(key) or "").split("|")
        if len(saved) != 2 or saved[0] != day:
            self.j.set(key, f"{day}|{st['equity']}")       # 그날 첫 실행 — 기준 자산 기록
            return
        limit = float(getattr(cfg, "daily_loss_pct", 0) or 0)
        budget = float(st.get("budget") or 0)
        if not limit or not budget:
            return
        chg = (float(st["equity"]) - float(saved[1])) / budget * 100
        if chg <= limit:
            self.no_buy_reason = f"하루 손실 한도 — 오늘 {chg:+.1f}% (한도 {limit}%)"
            self.say(f"🧯 {self.no_buy_reason} → 오늘은 새로 사지 않아요 (매도·손절은 그대로)")
            flag = f"daily_loss_notified_{cfg.ledger}"
            if self.j.get(flag) != day:
                self.j.set(flag, day)
                self.notify("halt", f"🧯 {self._mk()} 하루 손실 한도 도달 — 오늘 봇 손익 {chg:+.1f}% "
                                    f"(한도 {limit}%). 오늘은 새로 사지 않아요.")

    # ── 손절·익절 (정기 실행과 장중 감시가 함께 씀) ─────────
    def _exit_trigger(self, h, code: str | None = None, track: bool = True) -> str | None:
        pnl = round(h.pnl_pct, 6)              # 부동소수 오차(14.9999…%) 방지
        if pnl <= self._stop_for(code):
            return "손절"
        if pnl >= self.cfg.take_profit_pct:
            return "익절"
        if code and self._protect_hit(code, h, track):
            return "이익 보호"
        return None

    def _stop_for(self, code: str | None) -> float:
        """이 종목의 손절선(%). ATR 손절을 켰고 살 때(또는 첫 정기 실행 때) 정해 둔 값이 있으면 그것, 아니면 고정 %."""
        from . import strategy
        if code and strategy.settings(self.cfg)["stop_atr"]:
            opened = self.j.bot_codes(self.cfg.market).get(code) or ""
            saved = (self.j.get(f"stop_{self.cfg.ledger}_{code}") or "").split("|")
            if len(saved) == 2 and saved[0] == opened:
                try:
                    return float(saved[1])
                except ValueError:
                    pass
        return float(self.cfg.stop_loss_pct)

    def _set_atr_stops(self, candidates: list[dict], codes, k: float) -> None:
        """ATR 손절선을 정해 둔다 (방금 산 종목 · 아직 값이 없는 보유 종목). 한 번 정하면 팔 때까지 그대로."""
        from . import strategy
        if not k:
            return
        atr = {c["code"]: c.get("atr14_pct") for c in candidates}
        opened_map = self.j.bot_codes(self.cfg.market)
        for code in codes:
            key = f"stop_{self.cfg.ledger}_{code}"
            opened = opened_map.get(code) or now().date().isoformat()     # 방금 주문(체결 전)이면 오늘 연 것으로
            saved = (self.j.get(key) or "").split("|")
            if len(saved) == 2 and saved[0] == opened:
                continue
            if atr.get(code) is None:
                continue
            pct = strategy.stop_pct(self.cfg.stop_loss_pct, k, atr[code])
            self.j.set(key, f"{opened}|{pct}")
            self.say(f"📐 {code} ATR 손절선 {pct:+.1f}% (ATR {float(atr[code]):.1f}% × {k:g})")

    def _shadow_prices(self, quotes: dict) -> dict[str, float]:
        """그림자 보유 종목까지 포함한 지금 가격 (실제 봇이 안 본 종목은 따로 시세 조회)."""
        from . import shadow
        prices = {c: float(q.price) for c, q in quotes.items() if q and q.price}
        for code in shadow.positions(self.j, self.cfg.ledger):
            if code not in prices:
                try:
                    prices[code] = float(self.broker.quote(code).price)
                except Exception as e:  # noqa: BLE001
                    log.warning("그림자 시세 실패 %s: %s", code, e)
        return prices

    def _shadow_watch(self) -> None:
        """장중 감시 때 그림자(규칙만) 보유 종목의 손절·익절·이익 보호도 (실제 봇과 같은 조건으로 비교하려고)."""
        if self.cfg.ai_provider == "rules":
            return
        try:
            from . import shadow
            codes = list(shadow.positions(self.j, self.cfg.ledger))
            if not codes:
                return
            prices, open_ = {}, False
            for code in codes:
                q = self.broker.quote(code)
                prices[code] = float(q.price)
                open_ = open_ or q.market_status == "1"
            if open_:
                shadow.watch(self.j, self.cfg, prices, self.say)
        except Exception as e:  # noqa: BLE001
            log.warning("그림자 장중 감시 실패: %s", e)

    def _shadow_step(self, candidates: list[dict], quotes: dict, block: str | None) -> None:
        """규칙만 그림자 장부 — AI 없이 규칙 신호만 따랐다면 (주문 없음, 비교용 기록만)."""
        try:
            from . import shadow
            r = shadow.step(self.j, self.cfg, candidates, self._shadow_prices(quotes), block, self.say)
            if r["sold"] or r["bought"]:
                self.stage("그림자(규칙만)")
        except Exception as e:  # noqa: BLE001
            log.warning("그림자 장부 실패: %s", e)

    def _cooldown(self, days: int) -> dict[str, str]:
        """손실로 판 지 days 거래일이 안 된 종목 → 사유. 재매수 대기 (보호 규칙)."""
        if not days:
            return {}
        from . import ledger, strategy
        today = now().date().isoformat()
        last: dict[str, dict] = {}
        try:
            for t in ledger.trades(self.j, self.cfg.ledger, ledger.costs_of(self.cfg)):
                last[t["code"]] = t                      # 오래된 → 최신 순이라 마지막이 최근 매도
        except Exception as e:  # noqa: BLE001
            log.warning("재매수 대기 확인 실패: %s", e)
            return {}
        out = {}
        for code, t in last.items():
            if t["pnl"] < 0:
                n = strategy.busdays_between(t["date"], today)
                if n < days:
                    out[code] = f"{t['date'][5:]} 손실 매도 → {days - n}거래일 더"
        return out

    def _protect_hit(self, code: str, h, track: bool = True) -> bool:
        """이익 보호: 보유 중 고점을 기록해 두고, 수익이 protect_at % 를 넘은 적이 있으면
        max(본전, 고점 − trail %) 아래로 내려올 때 정리한다 (설정 → 전략 → 보호 규칙)."""
        from . import strategy
        opts = strategy.guard_opts(strategy.settings(self.cfg))
        if not opts or not opts.get("protect_at") or not h.avg_price or not h.now_price:
            return False
        key = f"peak_{self.cfg.ledger}_{code}"
        opened = self.j.bot_codes(self.cfg.market).get(code) or ""
        saved = (self.j.get(key) or "").split("|")
        peak = float(saved[1]) if len(saved) == 2 and saved[0] == opened else float(h.avg_price)   # 새로 산 종목은 처음부터
        if h.now_price > peak or len(saved) != 2 or saved[0] != opened:
            peak = max(peak, float(h.now_price))
            if track:
                self.j.set(key, f"{opened}|{peak}")
        floor = strategy.protect_floor(float(h.avg_price), peak, opts)
        if floor and h.now_price <= floor:
            self.say(f"🛡 {h.name} 고점 {self.cfg.money(peak)}({(peak / h.avg_price - 1) * 100:+.1f}%)까지 갔다가 "
                     f"보호선 {self.cfg.money(floor)} 아래로 내려옴")
            return True
        return False

    def _rule_exits(self, held: dict, prices: dict, bids: dict, tag: str = "") -> int:
        """손절은 시장가로, 하루 주문 한도와 상관없이 판다. 익절은 설정한 주문 방식·한도를 따른다."""
        from . import strategy
        max_hold = strategy.settings(self.cfg)["max_hold_days"]
        n = 0
        for c, h in list(held.items()):
            trig = self._exit_trigger(h, c)
            if not trig and max_hold and self.j.held_days(c) != 999 and self.j.held_days(c) >= max_hold:
                trig = "보유기간 만료"
                self.say(f"📏 {h.name} {self.j.held_days(c)}일 보유 (최대 {max_hold}일) → 정리")
            if not trig:
                continue
            if trig != "보유기간 만료":
                self.say(f"📏 {h.name} 수익률 {h.pnl_pct:+.2f}% → {trig}{tag} 규칙 발동")
            qty = min(h.qty, self.broker.sellable_qty(c)) if self.cfg.mode != "dry" else h.qty
            stop = trig in ("손절", "이익 보호")           # 지키는 매도는 시장가로, 하루 주문 한도와 상관없이
            if qty > 0 and self._exec("SELL", c, h.name, qty, bids.get(c) or prices.get(c) or h.now_price, trig + tag,
                                      force_market=stop, ignore_cap=stop):
                n += 1
                if qty >= h.qty:
                    held.pop(c)
        return n

    def watch(self) -> tuple[str, bool]:
        """장중 감시 — AI 없이 봇 보유 종목의 손절·익절만 확인. 반환: (출력, 무언가 했는지)"""
        cfg = self.cfg
        if cfg.stop_file.exists():
            return "킬스위치로 정지 중", False
        from . import fills, ledger
        closing = near_close(cfg.market)
        if closing:
            self.say(f"🔔 장 마감 {closing}분 전 — 남은 미체결 주문을 정리해요")
        try:
            # 평소엔 30분 넘게 안 된 주문만 취소, 장 마감 직전엔 남은 것 모두 취소 (넘겨도 장 끝나면 무효)
            cut = now() - timedelta(minutes=5 if closing else 30)
            fills.reconcile(self.j, cfg, self.broker, self.say, notify=self.notify,
                            cancel_before=cut.isoformat(timespec="seconds"))
        except Exception as e:  # noqa: BLE001
            self.say(f"⚠ 체결 확인 중 오류: {e}")
        bal = self.broker.balance()
        self.acct_qty = {c: int(h.qty) for c, h in bal.holdings.items()}
        from . import corpaction
        paused = corpaction.check(self.j, cfg, bal.holdings, self.say, self.notify) if cfg.mode != "dry" else set()
        if cfg.mode != "dry":
            paused |= fills.resolve_unsure(self.j, cfg, bal.holdings, self.say, self.notify)
            self.paused |= paused
        held = own_holdings(self.j, cfg, bal.holdings)
        for c in paused:
            held.pop(c, None)
        from . import strategy
        sg = strategy.settings(cfg)
        full = dict(held)
        if sg["core_pct"]:
            held.pop(sg["core_code"], None)              # 코어 ETF 는 손절·익절 대상 아님
        self._shadow_watch()
        if not held:
            return self._out(), bool(self.lines)
        prices, bids, open_ = {}, {}, False
        for c in held:
            try:
                q = self.broker.quote(c)
            except Exception as e:  # noqa: BLE001
                self.say(f"⚠ {c} 시세 조회 실패: {e}")
                continue
            prices[c], bids[c] = q.price, q.bid
            open_ = open_ or q.market_status == "1"
            held[c].now_price = q.price
        if not open_:
            return self._out(), bool(self.lines)       # 장 시간이 아니면 조용히 끝
        self.orders_left = cfg.max_orders_per_day - self.j.orders_today(cfg.ledger)
        n = self._rule_exits(held, prices, bids, tag="(장중 감시)")
        if self.sent:
            fills.time.sleep(3)
            fills.reconcile(self.j, cfg, self.broker, self.say)
        snap = self.j.snapshot(cfg.ledger) or {}
        st = ledger.status(self.j, cfg, own_holdings(self.j, cfg, self.broker.balance().holdings if n else bal.holdings),
                           bal.orderable_cash)
        if snap:
            snap.update({k: v for k, v in st.items() if k != "positions"})
            full.update(held)
            for c in [c for c in full if c not in held and c != sg.get("core_code")]:
                full.pop(c)                               # 이번에 판 종목은 빼고, 코어는 남긴다
            snap["positions"] = [{"code": c, "name": h.name, "qty": h.qty, "avg_price": h.avg_price,
                                  "now_price": h.now_price, "pnl_pct": round(h.pnl_pct, 2)} for c, h in full.items()]
            snap.pop("ts", None)
            self.j.save_snapshot(cfg.ledger, snap)
        return self._out(), bool(self.lines)

    # ── 메인 ────────────────────────────────────────────
    def run(self, force: bool = False, analyze_only: bool = False) -> str:
        cfg = self.cfg
        mode_label = {"dry": "드라이런(주문 안 냄)", "mock": "모의투자", "live": "🔴 실계좌"}[cfg.mode]
        market_label = {"kr": "🇰🇷 국내", "us": "🇺🇸 미국"}.get(cfg.market, cfg.market)
        self.say(f"[{now():%Y-%m-%d %H:%M}] {market_label} · 실행 모드: {mode_label}")
        self.stages, self._mark = [], time.monotonic()

        if cfg.stop_file.exists():
            self.say(f"⏸ 킬스위치 파일({cfg.stop_file.name})이 있어 중단합니다.")
            return self._out()
        if self.j.get(f"halted_{cfg.ledger}") == "1":
            self.say("⏸ 최대 손실 한도 도달로 정지된 상태입니다. 확인 후 `python run.py resume` 으로 해제하세요.")
            return self._out()

        # 0) 지난 주문 체결 확인 + 남은 미체결 주문 취소 (가격이 오래됨)
        from . import fills
        try:
            fills.reconcile(self.j, cfg, self.broker, self.say, notify=self.notify,
                            cancel_before=None if analyze_only else now().isoformat(timespec="seconds"))
            fills.sync_settled_fx(self.j, cfg, self.broker, self.say)      # 토스: 결제 환율로 확정
            from . import scorecard
            scorecard.update(self.j, cfg, self.broker, self.say)           # AI 성적표: 지난 신호의 이후 수익률
        except Exception as e:  # noqa: BLE001
            self.say(f"⚠ 체결 확인 중 오류: {e}")

        self.stage("체결 확인")
        # 1) 데이터 수집
        candidates, quotes = [], {}
        bal = self.broker.balance()
        self.acct_qty = {c: int(h.qty) for c, h in bal.holdings.items()}
        if cfg.mode != "dry":                    # 지난번 응답 없던 주문 — 계좌 수량으로 체결 여부 확인
            from . import fills as _f
            self.paused |= _f.resolve_unsure(self.j, cfg, bal.holdings, self.say, self.notify)
        bot_codes = self.j.bot_codes(cfg.market)
        parked = self.j.parked(cfg.market)
        if parked:
            self.say("🅿 다른 증권사에 남겨 둔 봇 종목: " + ", ".join(
                f"{p['name'] or p['code']} {p['qty'] or '?'}주({account_label(p['broker'])})" for p in parked)
                + " — 그 증권사로 돌아오면 다시 관리해요. 지금은 매매 안 함")
        self.paused |= {p["code"] for p in parked}
        if cfg.mode != "dry":  # 사용자가 직접 판 종목은 장부에서 정리
            for code in list(bot_codes):
                if code not in bal.holdings:
                    self.j.close_position(code)
                    bot_codes.pop(code)
        # 액면분할·병합: 봇 장부를 고치거나, 아직 반영 전이면 그 종목은 이번에 매매하지 않는다
        from . import corpaction
        self.paused |= corpaction.check(self.j, cfg, bal.holdings, self.say, self.notify) if cfg.mode != "dry" else set()
        # 봇이 산 수량만 봇 몫으로 본다 — 같은 종목을 사용자가 따로 갖고 있어도 그 주식은 건드리지 않는다
        held = own_holdings(self.j, cfg, bal.holdings, self.say)
        for c in self.paused:
            held.pop(c, None)
        user_owned = {c for c in bal.holdings if c not in bot_codes}   # 사용자가 직접 가진 종목 → 매수 안 함

        from . import barcache
        self.bar_stats = {}

        def collect(codes, source):
            for code in codes:
                if code in quotes:
                    continue
                try:
                    q = self.broker.quote(code)
                    ind = indicators.compute(barcache.daily(self.broker, code, q, cfg.data_dir, cfg.market,
                                                            80, self.bar_stats))
                except Exception as e:  # noqa: BLE001
                    self.say(f"⚠ {code} 데이터 조회 실패: {e}")
                    continue
                quotes[code] = q
                candidates.append({
                    "code": code, "name": q.name, "market": q.market, "sector": q.sector,
                    "source": "held" if code in held else source,   # core=직접 지정, auto=오늘 자동 선정
                    "price": q.price, "today_change_pct": q.change_pct,
                    "per": q.per or None, "pbr": q.pbr or None, "foreign_pct": q.foreign_pct,
                    "from_52w_high_pct": round((q.price / q.w52_high - 1) * 100, 1) if q.w52_high else None,
                    "warnings": q.warnings, "held": code in held,
                    "affordable": q.price <= cfg.max_per_position_krw, **ind,
                })

        self.stage("잔고")
        collect([c for c in list(held) + cfg.symbols if c not in self.paused], "core")
        hit = self.bar_stats.get("hit", 0)
        self.stage(f"시세·일봉 {len(quotes)}종목" + (f"(일봉 캐시 {hit})" if hit else ""))
        if not quotes:
            self.say("데이터를 하나도 받지 못해 종료합니다.")
            return self._out()

        market_open = any(q.market_status == "1" for q in quotes.values())
        if not market_open and not (force or analyze_only):
            self.say("💤 지금은 정규장이 아닙니다 (휴장일/장외 시간). 종료합니다."
                     + (" 미국 정규장은 한국시간 23:30~06:00 (서머타임 22:30~05:00)" if cfg.market == "us" else ""))
            return self._out()

        # 자동 선정 종목 (그날 첫 실행 때 검색, 실패하면 직전 목록)
        if cfg.auto_count > 0:
            from . import universe
            auto = universe.ensure_today(cfg, self.broker, self.j, say=self.say)
            collect([a["code"] for a in auto if a["code"] not in self.paused], "auto")
            self.stage("자동 선정")

        # 2) 장부 / 손실 한도
        for c, h in held.items():
            if c in quotes:
                h.now_price = quotes[c].price
        from . import ledger
        if getattr(self.broker, "fx", 0):                  # 같은 계좌 현금을 국내·미국이 나눠 쓸 때 환산용
            self.j.set("fx_usdkrw", str(self.broker.fx))
        st = ledger.status(self.j, cfg, held, bal.orderable_cash)
        self.avail = st["avail"]
        equity, dd = st["equity"], st["pnl_pct"]
        self.say(f"💰 예산 {cfg.money(st['budget'])} · 투자 중 {cfg.money(st['invested'])} ({st['positions']}종목)"
                 f" · 지금 살 수 있음 {cfg.money(st['avail'])}"
                 f" | 봇 손익 {cfg.money(st['pnl'])} ({dd:+.2f}%: 실현 {cfg.money(st['realized'])}"
                 f" · 평가 {cfg.money(st['unrealized'])})")
        sh = st.get("shared")
        if sh and sh.get("short"):
            other = "미국" if cfg.market == "kr" else "국내"
            how = ledger.PRIORITIES[sh.get("priority", "split")]
            if sh.get("priority") == "session":
                self.say(f"🔗 국내·미국 같은 계좌 · 시간대별: 지금은 {'국내' if cfg.market == 'kr' else '미국'} 시간이라"
                         f" 주문가능 현금 {cfg.money(st['account_cash'])}을 전부 이 시장이 써요 ({other} 몫은 떼어 두지 않음)")
            else:
                self.say(f"🔗 국내·미국 같은 계좌: 주문가능 현금 {cfg.money(st['account_cash'])}이 두 시장 남은 예산 합계보다 적어"
                         f" {how}로 나눠요 → 이 시장 {sh['pct']}% ({cfg.money(sh['cap'])}),"
                         f" {other} 몫 {100 - sh['pct']}% 남겨 둠")
        elif sh and sh.get("fx") is None:
            self.say("🔗 국내·미국 같은 계좌지만 아직 환율을 몰라 현금을 나누지 않았어요 (미국 실행 한 번 뒤부터 적용)")
        if cfg.mode != "dry" and st["account_cash"] is not None and st["account_cash"] < st["room"] \
                and not (sh and sh.get("short")):
            self.say(f"⚠ 계좌 주문가능 현금 {cfg.money(st['account_cash'])} < 남은 예산 {cfg.money(st['room'])}"
                     f" → {cfg.money(st['avail'])}까지만 매수합니다")
        self._usd_short_hint(st, quotes)
        run_id = self.j.start_run(cfg.ledger, equity)
        self.j.save_snapshot(cfg.ledger, {
            **st, "equity": equity, "pnl_pct": dd,
            "positions": [{"code": c, "name": h.name, "qty": h.qty, "avg_price": h.avg_price,
                           "now_price": h.now_price, "pnl_pct": round(h.pnl_pct, 2)} for c, h in held.items()],
            "account": account_view(bal, held),
        })
        if dd <= cfg.max_drawdown_pct:
            self.j.set(f"halted_{cfg.ledger}", "1")
            self.say(f"🛑 봇 손익 {dd:.1f}% — 최대 손실 한도({cfg.max_drawdown_pct}%) 도달. 신규 매매를 멈춥니다.")
            self.notify("halt", f"🛑 {self._mk()} 최대 손실 한도 도달 — 봇 손익 {dd:.1f}% ({cfg.money(st['pnl'])}). "
                                f"매매를 멈췄습니다. 확인 후 웹에서 ‘재개’하세요.")
            self.j.finish_run(run_id, f"🛑 최대 손실 한도 도달 ({dd:.1f}%) — 매매 멈춤", {"_provider": "", "decisions": []})
            return self._out()

        self.orders_left = 0 if analyze_only else cfg.max_orders_per_day - self.j.orders_today(cfg.ledger)

        # 2-1) 코어-위성: 예산의 core_pct% 는 코어 ETF 를 사서 계속 보유 (봇 매매·손절 대상에서 뺌)
        from . import strategy
        sg = strategy.settings(cfg)
        core_code = sg["core_code"] if sg["core_pct"] else None
        if core_code:
            core_h = held.pop(core_code, None)
            need = st["core_budget"] - st["core_invested"]
            if core_code in user_owned:
                self.say(f"🧱 코어 {core_code}: 직접 보유한 종목이라 봇이 사지 않아요 (다른 코어 ETF 를 고르세요)")
            elif need > 0 and not analyze_only:
                try:
                    q = self.broker.quote(core_code)
                    est = q.ask or q.price
                    cash = need if cfg.mode == "dry" or bal.orderable_cash == float("inf") else min(need, bal.orderable_cash)
                    qty = int(cash // est) if est else 0
                    if qty >= 1 and q.market_status == "1":
                        if cfg.mode != "dry":
                            qty = min(qty, self.broker.buyable_qty(core_code, est))
                        if qty >= 1 and self._exec("BUY", core_code, q.name, qty, est, "코어 적립", ignore_cap=True):
                            self.avail = max(0, min(self.avail, (bal.orderable_cash - qty * est)
                                                    if bal.orderable_cash != float("inf") else self.avail))
                    elif qty < 1:
                        self.say(f"🧱 코어 {q.name}: 남은 코어 예산 {cfg.money(need)} 로는 1주({cfg.money(est)})를 못 사요")
                except Exception as e:  # noqa: BLE001
                    self.say(f"⚠ 코어 ETF 매수 실패: {e}")
            if core_h:
                self.say(f"🧱 코어 {core_h.name} {core_h.qty}주 보유 중 ({core_h.pnl_pct:+.1f}%) — 매매 대상 아님")
            candidates = [c for c in candidates if c["code"] != core_code]

        # 2-2) 시장 필터: 지수가 200일선 아래면 새로 사지 않음
        self.no_buy_reason = None
        if sg["market_filter"]:
            ok, why = strategy.regime(self.broker, cfg)
            if ok is False:
                self.no_buy_reason = f"시장 필터 — {why}"
                self.say(f"🚦 {self.no_buy_reason} → 오늘은 새로 사지 않아요")
            elif ok is None:
                self.say(f"🚦 시장 필터 건너뜀 ({why})")

        shadow_block = self.no_buy_reason          # 그림자(규칙만)에도 같은 시장 필터 (하루 손실 한도는 봇 자신 기준이라 제외)

        # 2-3) 하루 손실 한도: 그날 첫 실행 때의 봇 자산 대비 예산의 N% 넘게 빠지면 그날은 새로 사지 않음
        self._daily_loss_check(st)

        # 2-4) 신고가 돌파: 장중 가격으로는 가짜 돌파가 많다 → 매수는 그날 마지막 정기 실행에서만
        gopts = strategy.guard_opts(sg)
        cooled = self._cooldown((gopts or {}).get("reentry_days") or 0)
        for c in candidates:
            if not c["held"] and c["code"] in cooled:
                c["cooldown"] = cooled[c["code"]]
        self._set_atr_stops(candidates, list(held), sg["stop_atr"])     # 예전에 산 종목도 ATR 손절선을 정해 둠
        if sg["signal"] == "breakout":
            if sg["late_buy"] and not self.no_buy_reason and not force:      # 직접 누른 강제 실행은 예외
                last = last_slot_wait(cfg)
                if last:
                    self.no_buy_reason = f"돌파 매수는 그날 마지막 실행({last})에서만"
                    self.say(f"⏳ {self.no_buy_reason} — 장중 가짜 돌파를 피하려고 지금은 새로 사지 않아요 (매도는 그대로)")
            for c in candidates:                       # 돌파해서 산 종목: 살 때의 돌파선 (돌파 실패 판단용)
                if c["held"]:
                    try:
                        c["breakout_level"] = float(self.j.get(f"bo_level_{cfg.ledger}_{c['code']}") or 0) or None
                    except ValueError:
                        c["breakout_level"] = None

        if sg["signal"] == "breakout" and sg["late_buy"] and not force and not shadow_block:
            last = last_slot_wait(cfg)
            shadow_block = f"돌파 매수는 마지막 실행({last})에서만" if last else None
        if not analyze_only and cfg.ai_provider != "rules" and getattr(self.analyst, "name", "") != "rules":
            self._shadow_step(candidates, quotes, shadow_block)

        # 3) 규칙 기반 강제 청산 (AI보다 먼저)
        if not analyze_only:
            self._rule_exits(held, {c: q.price for c, q in quotes.items()}, {c: q.bid for c, q in quotes.items()})
        else:
            for c, h in held.items():
                trig = self._exit_trigger(h, c, track=False)
                if trig:
                    self.say(f"📏 {h.name} 수익률 {h.pnl_pct:+.2f}% → {trig} 규칙 (분석 전용 — 주문 안 함)")

        # 4) 하이브리드: 규칙이 먼저 신호를 찾고, 신호 난 종목만 AI 에게 확인 (신호 없으면 AI 생략)
        allowed = None                       # None = 모든 후보 허용
        is_rules = getattr(self.analyst, "name", "") == "rules"
        if cfg.ai_hybrid and not is_rules:
            from . import signals
            cash_now = self.avail
            cheapest = min((c["price"] for c in candidates if not c["held"] and c["affordable"]), default=0)
            can_buy = (len(held) < cfg.max_positions and cheapest > 0 and cash_now >= cheapest
                       and not self.no_buy_reason)
            skipped: list = []
            sig = signals.evaluate(candidates, can_buy=can_buy, mode=sg["signal"], opts=gopts, skipped=skipped)
            if skipped:
                self.say("🚫 매수 신호 건너뜀 (추격 매수 차단·재매수 대기): "
                         + ", ".join(f"{c['name']}({why})" for c, why in skipped))
            if sg["rule_sell"] == "direct":
                n_sold = self._direct_rule_sells(sig, held, quotes, run_id, analyze_only)
                if n_sold is not None and not sig:
                    self.j.finish_run(run_id, f"규칙 매도 {n_sold}건 바로 실행 — 매수 신호 없어 AI 호출 생략",
                                      {"_provider": "규칙", "decisions": []})
                    return self._out()
            if not sig:
                why = "" if can_buy else " (매수 여력·빈 자리 없음 → 매도 신호만 확인)"
                self.say(f"📏 규칙 신호 없음{why} — AI 호출 생략 ({len(candidates)}종목 확인, 토큰 0)")
                self.j.finish_run(run_id, "규칙 신호 없음 — AI 호출 생략",
                                  {"_provider": "규칙 필터", "decisions": []})
                return self._out()
            for c in candidates:
                if c["code"] in sig:
                    c["rule_signal"] = sig[c["code"]]["reason"]
            sent = [c for c in candidates if c["code"] in sig]
            allowed = set(sig)
            self.say(f"📏 규칙 신호({signals.label(sg['signal'])}) {len(sent)}종목 → AI 확인 (전체 {len(candidates)}종목 중): "
                     + ", ".join(f"{c['name']}({'매도' if sig[c['code']]['type'].startswith('SELL') else '매수'})"
                                 for c in sent))
            candidates = sent

        # 5) AI 판단
        if cfg.ai_news and not is_rules and candidates:     # 뉴스·공시 (설정에서 켰을 때만)
            from . import news
            self.stage("규칙·판단 준비")
            k = news.enrich(candidates, cfg.market, cfg.data_dir, self.say)
            self.stage("뉴스·공시")
            self.say(f"📰 뉴스·공시: {k}/{len(candidates)}종목에 최근 소식 첨부")

        ctx = {
            "today": now().strftime("%Y-%m-%d %A"),
            "market": cfg.market.upper(),          # KR | US
            "currency": cfg.currency,              # 모든 금액·가격의 통화
            "rules": {
                "bot_budget": cfg.total_krw,
                "max_per_position": cfg.max_per_position_krw,
                "max_positions": cfg.max_positions,
                "auto_stop_loss_pct": cfg.stop_loss_pct,
                "auto_stop_loss_atr_mult": sg["stop_atr"] or None,   # 있으면 손절은 ATR14 × 배수 (2~15%)
                "auto_take_profit_pct": cfg.take_profit_pct,
                "min_hold_days": cfg.min_hold_days,
                "max_hold_days": sg["max_hold_days"],
                "signal_mode": sg["signal"],
                "signal_opts": gopts,
            },
            "portfolio": {
                "cash": self.avail,
                "equity": equity,
                "positions": [{
                    "code": c, "name": h.name, "qty": h.qty, "avg_price": h.avg_price,
                    "now_price": h.now_price, "pnl_pct": round(h.pnl_pct, 2),
                    "held_days": self.j.held_days(c),
                } for c, h in held.items()],
            },
            "candidates": candidates,
            "recent_decisions": self.j.recent_decisions(mode=cfg.ledger),
        }
        self.say(f"🧠 {getattr(self.analyst, 'label', 'AI')} 에게 {len(candidates)}종목 판단 요청 중…")
        self.stage("판단 준비")
        t_ai = time.monotonic()

        def say_time():
            total = round(time.monotonic() - t_ai)
            per = getattr(self.analyst, "timings", None)
            if per:
                self.say(f"⏱ AI 응답 시간: {' · '.join(per)} (전체 {total}초)")
            elif not is_rules:
                self.say(f"⏱ AI 응답 시간: {getattr(self.analyst, 'label', 'AI').split('(')[0]} {total}초")
        try:
            result = self.analyst.decide(ctx)
            self.stage("AI")
            say_time()
        except Exception as e:  # noqa: BLE001
            say_time()
            self.say(f"⚠ AI 분석 실패 — 이번 회차는 매매하지 않습니다: {e}")
            self.j.finish_run(run_id, f"⚠ AI 분석 실패 — 매매 안 함: {str(e)[:300]}",
                              {"_provider": getattr(self.analyst, "label", "AI"), "decisions": []})
            if not analyze_only:
                self.notify("errors", f"⚠ {self._mk()} AI 분석 실패 — 이번 회차는 매매하지 않았습니다\n{e}")
            return self._out()
        who = result.get("_provider") or getattr(self.analyst, "label", "AI")
        self.say(f"🧠 {who} 시장판단:\n" + _indent(result.get("market_view", ""), "  "))
        decisions = [d for d in result.get("decisions", []) if isinstance(d, dict)]

        # 6) 위험관리 필터 → 실행 (매도 먼저, 그다음 확신도 높은 매수)
        names = {c: q.name for c, q in quotes.items()}
        names.update({c: h.name for c, h in held.items()})
        self.j.remember_names(names)

        def note(d, outcome):
            nm = names.get(d.get("code")) or self.j.names().get(d.get("code")) or d.get("code")
            self.j.add_decision(run_id, d, outcome, nm if nm != d.get("code") else None)
            self.say(f"  · {d.get('action')} {nm} (확신 {float(d.get('confidence', 0)):.2f}) → {outcome}\n"
                     + _indent(d.get("reason", ""), "    └ ", "      "))

        from .config import norm_symbol
        for d in decisions:
            d["code"] = norm_symbol(d.get("code", ""), cfg.market)
            d["confidence"] = float(d.get("confidence") or 0)
        if allowed is not None:            # 하이브리드: 신호 난 종목만 매매 대상
            for d in [d for d in decisions if d["code"] not in allowed and d.get("action") != "HOLD"]:
                note(d, "무시 (규칙 신호 없는 종목)")
            decisions = [d for d in decisions if d["code"] in allowed]

        for d in [d for d in decisions if d.get("action") == "SELL"]:
            c = d["code"]
            if c not in held:
                note(d, "무시 (봇 보유 종목 아님)"); continue
            if d["confidence"] < cfg.min_confidence:
                note(d, "보류 (확신도 부족)"); continue
            if self.j.held_days(c) < cfg.min_hold_days:
                note(d, f"보류 (최소 보유 {cfg.min_hold_days}일 미달)"); continue
            if analyze_only:
                note(d, "분석 전용 — 주문 안 함"); continue
            h = held[c]
            ratio = min(max(float(d.get("sell_ratio") or 1.0), 0.0), 1.0)
            qty = max(1, math.floor(h.qty * ratio))
            if cfg.mode != "dry":
                qty = min(qty, self.broker.sellable_qty(c))
            if qty <= 0:
                note(d, "매도 가능 수량 없음"); continue
            ok = self._exec("SELL", c, h.name, qty, quotes[c].bid or quotes[c].price, "AI")
            note(d, "매도 실행" if ok else "매도 실패")
            if ok and qty >= h.qty:
                held.pop(c)

        if allowed is not None and not analyze_only:      # AI 성적표: 규칙 매수 신호를 AI 가 통과/차단했는지 남김
            try:
                from . import scorecard
                from .analyst import _short
                ai_buy = {d["code"] for d in decisions if d.get("action") == "BUY"
                          and d["confidence"] >= cfg.min_confidence}
                by_code = {d["code"]: d for d in decisions}
                me = _short(str(result.get("_provider") or "AI"))
                for c in candidates:
                    if not c["held"] and c["code"] in allowed:
                        d = by_code.get(c["code"]) or {}
                        votes = d.get("votes") or {me: d.get("action", "HOLD")}
                        scorecard.record(self.j, cfg.ledger, cfg.market, c["code"], c["name"], c["price"],
                                         c["code"] in ai_buy, c.get("rule_signal", ""), votes)
            except Exception as e:  # noqa: BLE001
                log.warning("AI 성적표 기록 실패: %s", e)
        levels = {c["code"]: c.get("high_20d_prev") for c in candidates} if sg["signal"] == "breakout" else {}

        cash = self.avail
        buys = sorted([d for d in decisions if d.get("action") == "BUY"], key=lambda d: -d["confidence"])
        for d in buys:
            c, q = d["code"], quotes.get(d["code"])
            if q is None:
                note(d, "무시 (후보 목록 밖)"); continue
            if c in held:
                note(d, "무시 (이미 보유 — 추가매수 안 함)"); continue
            if c in user_owned:
                note(d, "무시 (사용자가 직접 보유한 종목 — 섞이지 않게 매수 안 함)"); continue
            if self.no_buy_reason:
                note(d, f"보류 ({self.no_buy_reason})"); continue
            if c in cooled:
                note(d, f"보류 (재매수 대기 — {cooled[c]})"); continue
            if core_code and c == core_code:
                note(d, "무시 (코어 ETF — 따로 적립)"); continue
            if q.warnings:
                note(d, f"거부 (경고 종목: {', '.join(q.warnings)})"); continue
            if d["confidence"] < cfg.min_confidence:
                note(d, "보류 (확신도 부족)"); continue
            if len(held) >= cfg.max_positions:
                note(d, f"보류 (최대 보유 {cfg.max_positions}종목)"); continue
            est = q.ask or q.price
            budget = min(float(d.get("amount_krw") or cfg.max_per_position_krw), cfg.max_per_position_krw, cash)
            qty = int(budget // est) if est else 0
            if qty < 1:
                note(d, f"보류 (예산 {cfg.money(budget)}으로 1주({cfg.money(est)})도 못 삼)"); continue
            if analyze_only:
                note(d, f"분석 전용 — {qty}주 ≈{cfg.money(qty * est)} 매수 예정"); continue
            if cfg.mode != "dry":
                qty = min(qty, self.broker.buyable_qty(c, est))
                if qty < 1:
                    note(d, "보류 (증권사 주문가능수량 0)"); continue
            ok = self._exec("BUY", c, q.name, qty, est, "AI")
            note(d, "매수 실행" if ok else "매수 실패")
            if ok:
                self.j.set(f"bo_level_{cfg.ledger}_{c}", str(levels.get(c) or ""))     # 돌파선 (돌파 실패 판단용)
                self._set_atr_stops(candidates, [c], sg["stop_atr"])
                cash -= qty * est
                held[c] = None  # 보유 종목 수 카운트용

        for d in decisions:
            if d.get("action") == "HOLD":
                note(d, "유지")

        if self.sent:                      # 방금 보낸 주문의 즉시 체결분을 장부에 반영
            fills.time.sleep(3)
            try:
                fills.reconcile(self.j, cfg, self.broker, self.say)
            except Exception as e:  # noqa: BLE001
                self.say(f"⚠ 체결 확인 중 오류: {e}")

        self.stage("주문·체결 확인")
        self.j.finish_run(run_id, result.get("market_view", ""), result)
        if result.get("_usage"):
            u = result["_usage"]
            self.say(f"(AI 토큰 사용: 입력 {u['in']:,} / 출력 {u['out']:,})")
        return self._out()

    def _out(self) -> str:
        if self.stages and self._mark is not None:
            self.stage("마무리")
            parts = [f"{n} {round(t)}초" for n, t in self.stages if t >= 0.5]
            total = round(sum(t for _, t in self.stages))
            self.lines.append(f"⏱ 단계별 시간: {' · '.join(parts) or '모두 1초 미만'} (전체 {total}초)")
            self._mark = None
        return "\n".join(self.lines)
