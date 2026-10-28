"""한 번 실행 = 한 번의 '분석 → 위험관리 → 주문' 사이클. 스케줄러가 하루 1~2회 호출한다."""
from __future__ import annotations

import logging
import math

from . import indicators
from .config import Config
from .journal import Journal, now

log = logging.getLogger(__name__)


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
    def bot_cash(self) -> int:
        key = f"bot_cash_{self.cfg.mode}"
        v = self.j.get(key)
        if v is None:
            self.j.set(key, str(self.cfg.total_krw))
            return self.cfg.total_krw
        return int(float(v))

    def _add_cash(self, delta: int) -> None:
        self.j.set(f"bot_cash_{self.cfg.mode}", str(self.bot_cash + delta))

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
        self.j.add_order(self.cfg.mode, side, code, name, qty, est_price, res, trigger)
        if not res.ok:
            self.say(f"❌ {name} {side} {qty}주 실패: {res.message}")
            return False
        self.orders_left -= 1
        amt = qty * est_price
        self.say(f"✅ {'매수' if side == 'BUY' else '매도'} {name}({code}) {qty}주 ≈{amt:,}원 [{trigger}] "
                 f"주문번호 {res.order_no}")
        if self.cfg.mode != "dry":
            self._add_cash(-amt if side == "BUY" else amt)
            if side == "BUY":
                self.j.open_position(code, name)
        return True

    # ── 메인 ────────────────────────────────────────────
    def run(self, force: bool = False, analyze_only: bool = False) -> str:
        cfg = self.cfg
        mode_label = {"dry": "드라이런(주문 안 냄)", "mock": "모의투자", "live": "🔴 실계좌"}[cfg.mode]
        self.say(f"[{now():%Y-%m-%d %H:%M}] 실행 모드: {mode_label}")

        if cfg.stop_file.exists():
            self.say(f"⏸ 킬스위치 파일({cfg.stop_file.name})이 있어 중단합니다.")
            return self._out()
        if self.j.get(f"halted_{cfg.mode}") == "1":
            self.say("⏸ 최대 손실 한도 도달로 정지된 상태입니다. 확인 후 `python run.py resume` 으로 해제하세요.")
            return self._out()

        # 1) 데이터 수집
        candidates, quotes = [], {}
        bal = self.broker.balance()
        bot_codes = self.j.bot_codes()
        if cfg.mode != "dry":  # 사용자가 직접 판 종목은 장부에서 정리
            for code in list(bot_codes):
                if code not in bal.holdings:
                    self.j.close_position(code)
                    bot_codes.pop(code)
        held = {c: h for c, h in bal.holdings.items() if c in bot_codes}

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
            self.say("💤 지금은 정규장이 아닙니다 (휴장일/장외 시간). 종료합니다.")
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
        holdings_value = sum(h.value for h in held.values())
        equity = self.bot_cash + holdings_value
        dd = (equity / cfg.total_krw - 1) * 100
        self.say(f"💰 봇 자산 {equity:,}원 (현금 {self.bot_cash:,} + 주식 {holdings_value:,}) | 원금 대비 {dd:+.2f}%")
        run_id = self.j.start_run(cfg.mode, equity)
        self.j.save_snapshot(cfg.mode, {
            "equity": equity, "cash": self.bot_cash, "pnl_pct": round(dd, 2),
            "positions": [{"code": c, "name": h.name, "qty": h.qty, "avg_price": h.avg_price,
                           "now_price": h.now_price, "pnl_pct": round(h.pnl_pct, 2)} for c, h in held.items()],
        })
        if dd <= cfg.max_drawdown_pct:
            self.j.set(f"halted_{cfg.mode}", "1")
            self.say(f"🛑 원금 대비 {dd:.1f}% — 최대 손실 한도({cfg.max_drawdown_pct}%) 도달. 신규 매매를 멈춥니다.")
            return self._out()

        self.orders_left = 0 if analyze_only else cfg.max_orders_per_day - self.j.orders_today(cfg.mode)

        # 3) 규칙 기반 강제 청산 (AI보다 먼저)
        for c, h in list(held.items()):
            trig = None
            if h.pnl_pct <= cfg.stop_loss_pct:
                trig = "손절"
            elif h.pnl_pct >= cfg.take_profit_pct:
                trig = "익절"
            if trig:
                self.say(f"📏 {h.name} 수익률 {h.pnl_pct:+.2f}% → {trig} 규칙 발동")
                qty = min(h.qty, self.broker.sellable_qty(c)) if cfg.mode != "dry" else h.qty
                if qty > 0 and not analyze_only and self._exec("SELL", c, h.name, qty, quotes[c].bid or h.now_price, trig):
                    if qty >= h.qty:
                        self.j.close_position(c)
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
            "rules": {
                "bot_budget_krw": cfg.total_krw,
                "max_per_position_krw": cfg.max_per_position_krw,
                "max_positions": cfg.max_positions,
                "auto_stop_loss_pct": cfg.stop_loss_pct,
                "auto_take_profit_pct": cfg.take_profit_pct,
                "min_hold_days": cfg.min_hold_days,
            },
            "portfolio": {
                "cash_krw": min(self.bot_cash, bal.orderable_cash),
                "equity_krw": equity,
                "positions": [{
                    "code": c, "name": h.name, "qty": h.qty, "avg_price": h.avg_price,
                    "now_price": h.now_price, "pnl_pct": round(h.pnl_pct, 2),
                    "held_days": self.j.held_days(c),
                } for c, h in held.items()],
            },
            "candidates": candidates,
            "recent_decisions": self.j.recent_decisions(),
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
        def note(d, outcome):
            self.j.add_decision(run_id, d, outcome)
            q = quotes.get(d.get("code"))
            nm = q.name if q else d.get("code")
            self.say(f"  · {d.get('action')} {nm} (확신 {float(d.get('confidence', 0)):.2f}) → {outcome}\n"
                     f"    └ {d.get('reason', '')}")

        for d in decisions:
            d["code"] = str(d.get("code", "")).zfill(6)
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
                self.j.close_position(c)
                held.pop(c)

        cash = min(self.bot_cash, bal.orderable_cash) if cfg.mode != "dry" else self.bot_cash
        buys = sorted([d for d in decisions if d.get("action") == "BUY"], key=lambda d: -d["confidence"])
        for d in buys:
            c, q = d["code"], quotes.get(d["code"])
            if q is None:
                note(d, "무시 (후보 목록 밖)"); continue
            if c in held:
                note(d, "무시 (이미 보유 — 추가매수 안 함)"); continue
            if q.warnings:
                note(d, f"거부 (경고 종목: {', '.join(q.warnings)})"); continue
            if d["confidence"] < cfg.min_confidence:
                note(d, "보류 (확신도 부족)"); continue
            if len(held) >= cfg.max_positions:
                note(d, f"보류 (최대 보유 {cfg.max_positions}종목)"); continue
            est = q.ask or q.price
            budget = min(int(d.get("amount_krw") or cfg.max_per_position_krw), cfg.max_per_position_krw, cash)
            qty = budget // est if est else 0
            if qty < 1:
                note(d, f"보류 (예산 {budget:,}원으로 1주({est:,}원)도 못 삼)"); continue
            if analyze_only:
                note(d, f"분석 전용 — {qty}주 ≈{qty * est:,}원 매수 예정"); continue
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
