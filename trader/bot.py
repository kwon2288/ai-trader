"""한 번 실행 = 한 번의 '분석 → 위험관리 → 주문' 사이클. 스케줄러가 하루 1~2회 호출한다."""
from __future__ import annotations

import logging
import math

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
        if bq is not None and h.qty > bq:
            if say:
                say(f"🔒 {h.name}: 계좌 {h.qty}주 중 봇 몫 {bq}주만 관리 (나머지 {h.qty - bq}주는 사용자 보유)")
            h = Holding(c, h.name, bq, ba or h.avg_price, h.now_price)
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


class Bot:
    def __init__(self, cfg: Config, broker, analyst, journal: Journal | None = None):
        self.cfg, self.broker, self.analyst = cfg, broker, analyst
        self.j = journal or Journal(cfg.db_path)
        self.lines: list[str] = []
        self.orders_left = 0

    def say(self, s: str) -> None:
        log.info(s)
        self.lines.append(s)

    # ── 봇 전용 현금 장부 ───────────────────────────────
    @property
    def bot_cash(self):
        key = f"bot_cash_{self.cfg.ledger}"
        v = self.j.get(key)
        if v is None:
            self.j.set(key, str(self.cfg.total_krw))
            v = self.cfg.total_krw
        return round(float(v), 2) if self.cfg.currency == "USD" else int(float(v))

    def _sync_budget(self) -> None:
        """웹에서 예산(원금)을 바꾸면 그 차이만큼 봇 현금을 늘리거나 줄인다."""
        cfg, j = self.cfg, self.j
        bkey, ckey = f"bot_budget_{cfg.ledger}", f"bot_cash_{cfg.ledger}"
        total = float(cfg.total_krw)
        prev = j.get(bkey)
        if prev is None:
            # 예산 기록이 없던 예전 장부: 현금 + 지금까지 쓴 돈 = 처음 예산
            prev = (float(j.get(ckey)) + j.net_spent(cfg.ledger)) if j.get(ckey) is not None else total
        prev = float(prev)
        if abs(prev - total) > 0.005 and j.get(ckey) is not None:
            self._add_cash(total - prev)
            self.say(f"💱 원금 변경 반영: {cfg.money(prev)} → {cfg.money(total)} (봇 현금 {cfg.money(self.bot_cash)})")
        j.set(bkey, str(total))

    def _add_cash(self, delta) -> None:
        new = self.bot_cash + delta
        self.j.set(f"bot_cash_{self.cfg.ledger}", str(round(new, 2) if self.cfg.currency == "USD" else int(new)))

    # ── 주문 실행 (공통) ────────────────────────────────
    def _exec(self, side: str, code: str, name: str, qty: int, est_price: int, trigger: str) -> bool:
        if self.orders_left <= 0:
            self.say(f"⛔ {name} {side} 건너뜀 — 오늘 주문 한도 소진")
            return False
        price = None
        if self.cfg.order_type == "limit":
            price = est_price
        fn = self.broker.buy if side == "BUY" else self.broker.sell
        res = fn(code, qty, price)
        self.j.add_order(self.cfg.ledger, side, code, name, qty, est_price, res, trigger)
        if not res.ok:
            self.say(f"❌ {name} {side} {qty}주 실패: {res.message}")
            return False
        self.orders_left -= 1
        amt = round(qty * est_price, 2)
        self.say(f"✅ {'매수' if side == 'BUY' else '매도'} {name}({code}) {qty}주 ≈{self.cfg.money(amt)} [{trigger}] "
                 f"주문번호 {res.order_no}")
        if self.cfg.mode != "dry":
            self._add_cash(-amt if side == "BUY" else amt)
            if side == "BUY":
                self.j.open_position(code, name, self.cfg.market, qty, est_price)
            else:
                self.j.reduce_position(code, qty)
        return True

    # ── 메인 ────────────────────────────────────────────
    def run(self, force: bool = False, analyze_only: bool = False) -> str:
        cfg = self.cfg
        mode_label = {"dry": "드라이런(주문 안 냄)", "mock": "모의투자", "live": "🔴 실계좌"}[cfg.mode]
        market_label = {"kr": "🇰🇷 국내", "us": "🇺🇸 미국"}.get(cfg.market, cfg.market)
        self.say(f"[{now():%Y-%m-%d %H:%M}] {market_label} · 실행 모드: {mode_label}")

        if cfg.stop_file.exists():
            self.say(f"⏸ 킬스위치 파일({cfg.stop_file.name})이 있어 중단합니다.")
            return self._out()
        if self.j.get(f"halted_{cfg.ledger}") == "1":
            self.say("⏸ 최대 손실 한도 도달로 정지된 상태입니다. 확인 후 `python run.py resume` 으로 해제하세요.")
            return self._out()

        self._sync_budget()

        # 1) 데이터 수집
        candidates, quotes = [], {}
        bal = self.broker.balance()
        bot_codes = self.j.bot_codes(cfg.market)
        if cfg.mode != "dry":  # 사용자가 직접 판 종목은 장부에서 정리
            for code in list(bot_codes):
                if code not in bal.holdings:
                    self.j.close_position(code)
                    bot_codes.pop(code)
        # 봇이 산 수량만 봇 몫으로 본다 — 같은 종목을 사용자가 따로 갖고 있어도 그 주식은 건드리지 않는다
        held = own_holdings(self.j, cfg, bal.holdings, self.say)
        user_owned = {c for c in bal.holdings if c not in bot_codes}   # 사용자가 직접 가진 종목 → 매수 안 함

        def collect(codes, source):
            for code in codes:
                if code in quotes:
                    continue
                try:
                    q = self.broker.quote(code)
                    ind = indicators.compute(self.broker.daily(code, 80))
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

        collect(list(held) + cfg.symbols, "core")
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
            collect([a["code"] for a in auto], "auto")

        # 2) 장부 / 손실 한도
        for c, h in held.items():
            if c in quotes:
                h.now_price = quotes[c].price
        holdings_value = round(sum(h.value for h in held.values()), 2)
        equity = round(self.bot_cash + holdings_value, 2)
        dd = (equity / cfg.total_krw - 1) * 100
        self.say(f"💰 봇 자산 {cfg.money(equity)} (현금 {cfg.money(self.bot_cash)} + 주식 {cfg.money(holdings_value)})"
                 f" | 원금 대비 {dd:+.2f}%")
        if cfg.mode != "dry" and bal.orderable_cash != float("inf") and bal.orderable_cash < self.bot_cash:
            self.say(f"⚠ 계좌 주문가능 현금 {cfg.money(bal.orderable_cash)} < 봇 예산 현금 {cfg.money(self.bot_cash)}"
                     f" → 실제로는 {cfg.money(bal.orderable_cash)}까지만 매수합니다")
        run_id = self.j.start_run(cfg.ledger, equity)
        self.j.save_snapshot(cfg.ledger, {
            "equity": equity, "cash": self.bot_cash, "pnl_pct": round(dd, 2),
            "positions": [{"code": c, "name": h.name, "qty": h.qty, "avg_price": h.avg_price,
                           "now_price": h.now_price, "pnl_pct": round(h.pnl_pct, 2)} for c, h in held.items()],
            "account": account_view(bal, held),
        })
        if dd <= cfg.max_drawdown_pct:
            self.j.set(f"halted_{cfg.ledger}", "1")
            self.say(f"🛑 원금 대비 {dd:.1f}% — 최대 손실 한도({cfg.max_drawdown_pct}%) 도달. 신규 매매를 멈춥니다.")
            return self._out()

        self.orders_left = 0 if analyze_only else cfg.max_orders_per_day - self.j.orders_today(cfg.ledger)

        # 3) 규칙 기반 강제 청산 (AI보다 먼저)
        for c, h in list(held.items()):
            trig = None
            pnl = round(h.pnl_pct, 6)              # 부동소수 오차(14.9999…%) 방지
            if pnl <= cfg.stop_loss_pct:
                trig = "손절"
            elif pnl >= cfg.take_profit_pct:
                trig = "익절"
            if trig:
                self.say(f"📏 {h.name} 수익률 {h.pnl_pct:+.2f}% → {trig} 규칙 발동")
                qty = min(h.qty, self.broker.sellable_qty(c)) if cfg.mode != "dry" else h.qty
                if qty > 0 and not analyze_only and self._exec("SELL", c, h.name, qty, quotes[c].bid or h.now_price, trig):
                    if qty >= h.qty:
                        held.pop(c)

        # 4) 하이브리드: 규칙이 먼저 신호를 찾고, 신호 난 종목만 AI 에게 확인 (신호 없으면 AI 생략)
        allowed = None                       # None = 모든 후보 허용
        is_rules = getattr(self.analyst, "name", "") == "rules"
        if cfg.ai_hybrid and not is_rules:
            from . import signals
            cash_now = min(self.bot_cash, bal.orderable_cash) if cfg.mode != "dry" else self.bot_cash
            cheapest = min((c["price"] for c in candidates if not c["held"] and c["affordable"]), default=0)
            can_buy = len(held) < cfg.max_positions and cheapest > 0 and cash_now >= cheapest
            sig = signals.evaluate(candidates, can_buy=can_buy)
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
            self.say(f"📏 규칙 신호 {len(sent)}종목 → AI 확인 (전체 {len(candidates)}종목 중): "
                     + ", ".join(f"{c['name']}({'매도' if sig[c['code']]['type'].startswith('SELL') else '매수'})"
                                 for c in sent))
            candidates = sent

        # 5) AI 판단
        ctx = {
            "today": now().strftime("%Y-%m-%d %A"),
            "market": cfg.market.upper(),          # KR | US
            "currency": cfg.currency,              # 모든 금액·가격의 통화
            "rules": {
                "bot_budget": cfg.total_krw,
                "max_per_position": cfg.max_per_position_krw,
                "max_positions": cfg.max_positions,
                "auto_stop_loss_pct": cfg.stop_loss_pct,
                "auto_take_profit_pct": cfg.take_profit_pct,
                "min_hold_days": cfg.min_hold_days,
            },
            "portfolio": {
                "cash": min(self.bot_cash, bal.orderable_cash),
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
        try:
            result = self.analyst.decide(ctx)
        except Exception as e:  # noqa: BLE001
            self.say(f"⚠ AI 분석 실패 — 이번 회차는 매매하지 않습니다: {e}")
            return self._out()
        who = result.get("_provider") or getattr(self.analyst, "label", "AI")
        self.say(f"🧠 {who} 시장판단: {result.get('market_view', '')}")
        decisions = [d for d in result.get("decisions", []) if isinstance(d, dict)]

        # 6) 위험관리 필터 → 실행 (매도 먼저, 그다음 확신도 높은 매수)
        names = {c: q.name for c, q in quotes.items()}
        names.update({c: h.name for c, h in held.items()})
        self.j.remember_names(names)

        def note(d, outcome):
            nm = names.get(d.get("code")) or self.j.names().get(d.get("code")) or d.get("code")
            self.j.add_decision(run_id, d, outcome, nm if nm != d.get("code") else None)
            self.say(f"  · {d.get('action')} {nm} (확신 {float(d.get('confidence', 0)):.2f}) → {outcome}\n"
                     f"    └ {d.get('reason', '')}")

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

        cash = min(self.bot_cash, bal.orderable_cash) if cfg.mode != "dry" else self.bot_cash
        buys = sorted([d for d in decisions if d.get("action") == "BUY"], key=lambda d: -d["confidence"])
        for d in buys:
            c, q = d["code"], quotes.get(d["code"])
            if q is None:
                note(d, "무시 (후보 목록 밖)"); continue
            if c in held:
                note(d, "무시 (이미 보유 — 추가매수 안 함)"); continue
            if c in user_owned:
                note(d, "무시 (사용자가 직접 보유한 종목 — 섞이지 않게 매수 안 함)"); continue
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
                cash -= qty * est
                held[c] = None  # 보유 종목 수 카운트용

        for d in decisions:
            if d.get("action") == "HOLD":
                note(d, "유지")

        self.j.finish_run(run_id, result.get("market_view", ""), result)
        if result.get("_usage"):
            u = result["_usage"]
            self.say(f"(AI 토큰 사용: 입력 {u['in']:,} / 출력 {u['out']:,})")
        return self._out()

    def _out(self) -> str:
        return "\n".join(self.lines)
