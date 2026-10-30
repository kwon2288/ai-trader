"""한 번 실행 = 한 번의 '분석 → 위험관리 → 주문' 사이클. 스케줄러가 하루 1~2회 호출한다."""
from __future__ import annotations

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


class Bot:
    def __init__(self, cfg: Config, broker, analyst, journal: Journal | None = None):
        self.cfg, self.broker, self.analyst = cfg, broker, analyst
        self.j = journal or Journal(cfg.db_path)
        self.lines: list[str] = []
        self.orders_left = 0
        self.sent = 0          # 이번 실행에서 보낸(체결 확인할) 주문 수
        self.no_buy_reason = None

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
        res = fn(code, qty, price)
        from . import fills
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

    # ── 손절·익절 (정기 실행과 장중 감시가 함께 씀) ─────────
    def _exit_trigger(self, h) -> str | None:
        pnl = round(h.pnl_pct, 6)              # 부동소수 오차(14.9999…%) 방지
        if pnl <= self.cfg.stop_loss_pct:
            return "손절"
        if pnl >= self.cfg.take_profit_pct:
            return "익절"
        return None

    def _rule_exits(self, held: dict, prices: dict, bids: dict, tag: str = "") -> int:
        """손절은 시장가로, 하루 주문 한도와 상관없이 판다. 익절은 설정한 주문 방식·한도를 따른다."""
        from . import strategy
        max_hold = strategy.settings(self.cfg)["max_hold_days"]
        n = 0
        for c, h in list(held.items()):
            trig = self._exit_trigger(h)
            if not trig and max_hold and self.j.held_days(c) != 999 and self.j.held_days(c) >= max_hold:
                trig = "보유기간 만료"
                self.say(f"📏 {h.name} {self.j.held_days(c)}일 보유 (최대 {max_hold}일) → 정리")
            if not trig:
                continue
            if trig != "보유기간 만료":
                self.say(f"📏 {h.name} 수익률 {h.pnl_pct:+.2f}% → {trig}{tag} 규칙 발동")
            qty = min(h.qty, self.broker.sellable_qty(c)) if self.cfg.mode != "dry" else h.qty
            stop = trig == "손절"
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
        try:
            fills.reconcile(self.j, cfg, self.broker, self.say, notify=self.notify,
                            cancel_before=(now() - timedelta(minutes=30)).isoformat(timespec="seconds"))
        except Exception as e:  # noqa: BLE001
            self.say(f"⚠ 체결 확인 중 오류: {e}")
        bal = self.broker.balance()
        held = own_holdings(self.j, cfg, bal.holdings)
        from . import strategy
        sg = strategy.settings(cfg)
        full = dict(held)
        if sg["core_pct"]:
            held.pop(sg["core_code"], None)              # 코어 ETF 는 손절·익절 대상 아님
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
        except Exception as e:  # noqa: BLE001
            self.say(f"⚠ 체결 확인 중 오류: {e}")

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
        from . import ledger
        st = ledger.status(self.j, cfg, held, bal.orderable_cash)
        self.avail = st["avail"]
        equity, dd = st["equity"], st["pnl_pct"]
        self.say(f"💰 예산 {cfg.money(st['budget'])} · 투자 중 {cfg.money(st['invested'])} ({st['positions']}종목)"
                 f" · 지금 살 수 있음 {cfg.money(st['avail'])}"
                 f" | 봇 손익 {cfg.money(st['pnl'])} ({dd:+.2f}%: 실현 {cfg.money(st['realized'])}"
                 f" · 평가 {cfg.money(st['unrealized'])})")
        if cfg.mode != "dry" and st["account_cash"] is not None and st["account_cash"] < st["room"]:
            self.say(f"⚠ 계좌 주문가능 현금 {cfg.money(st['account_cash'])} < 남은 예산 {cfg.money(st['room'])}"
                     f" → {cfg.money(st['avail'])}까지만 매수합니다")
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

        # 3) 규칙 기반 강제 청산 (AI보다 먼저)
        if not analyze_only:
            self._rule_exits(held, {c: q.price for c, q in quotes.items()}, {c: q.bid for c, q in quotes.items()})
        else:
            for c, h in held.items():
                trig = self._exit_trigger(h)
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
            sig = signals.evaluate(candidates, can_buy=can_buy, mode=sg["signal"])
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
            k = news.enrich(candidates, cfg.market, cfg.data_dir, self.say)
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
                "auto_take_profit_pct": cfg.take_profit_pct,
                "min_hold_days": cfg.min_hold_days,
                "max_hold_days": sg["max_hold_days"],
                "signal_mode": sg["signal"],
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
        try:
            result = self.analyst.decide(ctx)
        except Exception as e:  # noqa: BLE001
            self.say(f"⚠ AI 분석 실패 — 이번 회차는 매매하지 않습니다: {e}")
            if not analyze_only:
                self.notify("errors", f"⚠ {self._mk()} AI 분석 실패 — 이번 회차는 매매하지 않았습니다\n{e}")
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

        self.j.finish_run(run_id, result.get("market_view", ""), result)
        if result.get("_usage"):
            u = result["_usage"]
            self.say(f"(AI 토큰 사용: 입력 {u['in']:,} / 출력 {u['out']:,})")
        return self._out()

    def _out(self) -> str:
        return "\n".join(self.lines)
