"""매매 사이클 실행 공통부 — 명령줄(run.py)·타이머·웹이 모두 이걸 쓴다.

파일 잠금으로 동시에 두 번 돌지 않게 막는다 (타이머 실행 중 웹에서 누르는 경우 등).
"""
from __future__ import annotations

import fcntl
import logging
import os
from contextlib import contextmanager

from .broker import BrokerError
from .config import Config
from .journal import KST, Journal

log = logging.getLogger(__name__)


class Busy(RuntimeError):
    pass


@contextmanager
def run_lock(cfg: Config, what: str = "실행", market: str = ""):
    from . import runstatus
    fh = open(cfg.data_dir / "run.lock", "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        raise Busy("다른 실행이 진행 중입니다. 잠시 후 다시 시도하세요.") from None
    runstatus.start(cfg.data_dir, what, market)
    try:
        yield
    finally:
        runstatus.end(cfg.data_dir)
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


def broker_name(cfg: Config) -> str:
    """시장별 증권사. 국내 BROKER(nh|kis|toss), 미국 BROKER_US(nh|toss, 기본은 국내와 같거나 nh)."""
    kr = os.environ.get("BROKER", "nh").strip().lower()
    if cfg.market == "kr":
        return kr
    return os.environ.get("BROKER_US", kr if kr in ("nh", "toss") else "nh").strip().lower()


def make_broker(cfg: Config):
    name = broker_name(cfg)
    if name == "toss":
        from .toss import TossBroker
        return TossBroker(cfg.mode, cfg.market, cfg.data_dir)
    if name == "kis":
        if cfg.market != "kr":
            raise BrokerError("한국투자증권(kis)은 이 프로그램에서 국내 주식만 지원합니다 → BROKER_US=nh 또는 toss")
        from .kis import KISBroker
        return KISBroker(cfg.mode, cfg.data_dir)
    if name != "nh":
        raise BrokerError(f"증권사는 nh / kis / toss 중 하나여야 합니다 (현재: {name})")
    try:
        if cfg.market == "us":
            from .nh_us import NHUSBroker
            return NHUSBroker(cfg.mode, margin_currency=str(cfg.us.get("margin_currency", "krw")))
        from .broker import NHBroker
        return NHBroker(cfg.mode)
    except ImportError:
        raise BrokerError("nhplug 패키지가 없습니다 → .venv/bin/pip install -r requirements.txt") from None


def market_cfg(cfg: Config, market: str) -> Config:
    if market == "us" and not cfg.us_enabled:
        raise BrokerError("미국 주식이 꺼져 있습니다 → config.toml 의 [us] enabled = true (또는 웹 설정)")
    return cfg.for_market(market)


def live_allowed(cfg: Config) -> bool:
    """실계좌 주문 허용: .env 의 I_UNDERSTAND_REAL_MONEY=yes 또는 웹에서 확인 절차를 거쳐 전환한 경우."""
    if cfg.mode != "live":
        return True
    return os.environ.get("I_UNDERSTAND_REAL_MONEY") == "yes" or (cfg.mode_source == "web" and cfg.live_ack)


def run_cycle(cfg: Config, analyze_only: bool = False, force: bool = False, source: str = "cli",
              market: str = "kr") -> str:
    """한 사이클 실행 후 결과 텍스트 반환. 결과는 일지(run_logs)에도 남긴다."""
    kind = "analyze" if analyze_only else "trade"
    with run_lock(cfg, "AI 분석(주문 없음)" if analyze_only else ("정기 매매" if source == "timer" else "매매 실행"), market):
        j = Journal(cfg.db_path)
        try:
            cfg = market_cfg(cfg, market)
            if not analyze_only and not live_allowed(cfg):
                raise BrokerError("실계좌 모드는 웹 설정에서 확인 절차를 거쳐 전환하거나 .env 에 I_UNDERSTAND_REAL_MONEY=yes 가 있어야 실행됩니다")
            from . import analyst as _an
            from .analyst import make_analyst
            from .bot import Bot
            led = cfg.ledger
            _an.USAGE_SINK = lambda p, m, ti, to: Journal(cfg.db_path).add_usage(led, p, m, ti, to, source)
            from . import pricing
            ms = pricing.month_status(j, cfg.data_dir)
            analyst = make_analyst(cfg)
            note = None
            if ms["over"] and getattr(analyst, "name", "") != "rules":
                from .signals import RulesAnalyst
                analyst = RulesAnalyst()
                note = (f"💸 이번 달 AI 비용 ${ms['spend']:.2f} ≥ 월 한도 ${ms['limit']:g} → 이번 달은 AI 없이 규칙만으로 실행 "
                        f"(사용량 화면에서 한도를 올리거나 0 으로 끄면 다시 AI 사용)")
                from . import notify as _nt
                if j.get("ai_limit_notified") != ms["month"]:
                    j.set("ai_limit_notified", ms["month"])
                    _nt.send(cfg, "errors", note)
            broker = make_broker(cfg)
            bot = Bot(cfg, broker, analyst, j)
            out = bot.run(force=force, analyze_only=analyze_only)
            try:                                        # 지수 종가 (손익 화면·주간 리포트의 지수 비교용, 하루 한 번)
                from . import bench
                bench.refresh(cfg, broker, j)
            except Exception as e:  # noqa: BLE001
                log.warning("지수 비교 데이터 갱신 실패: %s", e)
            if note:
                out = note + "\n" + out
        except Exception as e:  # noqa: BLE001
            out = f"❌ 실행 오류: {e}"
            if not analyze_only:
                from . import notify
                flag = {"kr": "🇰🇷", "us": "🇺🇸"}.get(cfg.market, "")
                notify.send(cfg, "errors", f"⚠ {flag} 자동매매 실행 오류\n{e}")
        j.add_log(cfg.ledger, kind, source, out)
        if not analyze_only and source == "timer":
            from . import monitor
            monitor.ping(cfg, ok=not out.startswith("❌"))
            try:                                        # 하루 한 번 매매 기록 백업
                import tomllib
                from . import backup
                from .config import ensure_config
                from . import notify as _nt
                backup.daily(j, cfg.data_dir, tomllib.loads(ensure_config().read_text(encoding="utf-8")),
                             warn=lambda m: _nt.send(cfg, "errors", m))
            except Exception as e:  # noqa: BLE001
                j.add_log(cfg.ledger, "backup", source, f"⚠ 백업 실패: {e}")
        return out


def run_watch(cfg: Config, market: str = "kr", source: str = "timer") -> str:
    """장중 감시 (AI 없음) — 다른 실행 중이면 조용히 건너뛴다. 무언가 했을 때만 실행 로그에 남긴다."""
    try:
        lock = run_lock(cfg, "장중 감시", market)
        lock.__enter__()
    except Busy:
        return "다른 실행 중 — 건너뜀"
    try:
        j = Journal(cfg.db_path)
        try:
            cfg = market_cfg(cfg, market)
            if not live_allowed(cfg):
                return "실계좌 확인 절차 전 — 건너뜀"
            if j.get(f"halted_{cfg.ledger}") == "1":
                pass                                  # 손실 한도 정지 중에도 손절은 계속 지킨다
            from .bot import Bot
            out, acted = Bot(cfg, make_broker(cfg), None, j).watch()
        except Exception as e:  # noqa: BLE001
            out, acted = f"❌ 장중 감시 오류: {e}", True
            from . import notify
            last = j.get(f"watch_err_{cfg.ledger}")
            if last != str(e)[:200]:                 # 같은 오류는 한 번만 알림
                notify.send(cfg, "errors", f"⚠ {'🇺🇸' if cfg.market == 'us' else '🇰🇷'} 장중 감시 오류\n{e}")
                j.set(f"watch_err_{cfg.ledger}", str(e)[:200])
        else:
            j.set(f"watch_err_{cfg.ledger}", "")
            try:                                       # 장중 감시 때 정기 실행 누락도 확인
                import tomllib
                from . import monitor, notify
                from .config import ensure_config
                monitor.check(j, cfg, tomllib.loads(ensure_config().read_text(encoding="utf-8")), notify.send)
            except Exception:  # noqa: BLE001
                pass
            j.set(f"watch_last_{cfg.ledger}", __import__("datetime").datetime.now(KST).isoformat(timespec="seconds"))
        if acted and out:
            j.add_log(cfg.ledger, "watch", source, out)
        return out
    finally:
        lock.__exit__(None, None, None)


def close_summary(base: Config, market: str, at=None, force: bool = False) -> bool:
    """장 마감 10분 뒤 하루 요약 알림 (국내 15:40, 미국 뉴욕 16:10 이후). 하루 한 번.
    웹의 백그라운드 감시(5분마다)가 부른다. 그날 자동 실행 기록이 없으면(휴장일 등) 보내지 않는다.
    보내기 전에 잔고를 새로 조회해 마감 가격으로 요약한다."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from . import ledger, notify, pricing
    from .bot import CLOSE
    cfg = market_cfg(base, market)
    if not notify.enabled(cfg, "daily") or not notify.channels(cfg.data_dir):
        return False
    tzname, hh, mm = CLOSE[market]
    tz = ZoneInfo(tzname)
    now = (at or datetime.now(tz)).astimezone(tz)
    if not force and (now.weekday() >= 5 or (now.hour, now.minute) < (hh, mm + 10)):
        return False
    j = Journal(cfg.db_path)
    key = f"daily_sent_{cfg.ledger}"
    if not force and j.get(key) == now.date().isoformat():
        return False
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    from .journal import KST
    ran = j.db.execute("SELECT 1 FROM run_logs WHERE mode=? AND kind='trade' AND ts >= ? LIMIT 1",
                       (cfg.ledger, start.astimezone(KST).isoformat(timespec="seconds"))).fetchone()
    if not ran and not force:
        return False
    j.set(key, now.date().isoformat())
    try:
        refresh_balance(base, market)                 # 마감 가격으로 다시 계산
    except Exception:  # noqa: BLE001
        pass
    try:                                              # 지수도 마감 종가로
        from . import bench
        bench.refresh(cfg, make_broker(cfg), j, force=True)
    except Exception as e:  # noqa: BLE001
        log.warning("지수 비교 데이터 갱신 실패: %s", e)
    snap = j.snapshot(cfg.ledger) or {}
    today = ledger.period(j, cfg.ledger, now.date(), now.date(), ledger.costs_of(cfg))
    ai = pricing.ai_cost(j, cfg.data_dir, cfg.ledger, start.astimezone(KST))      # 그 시장 하루(미국은 뉴욕 0시부터)
    flag = {"kr": "🇰🇷 국내", "us": "🇺🇸 미국"}.get(market, "")
    lines = [f"📊 {flag} 오늘 마감 요약 ({now:%m/%d})",
             f"오늘 주문: 매수 {today['buys']}건 · 매도 {today['n']}건",
             f"오늘 실현 손익: {cfg.money(today['pnl'])}"]
    if snap.get("budget") is not None:
        lines.append(f"봇 손익(누적): {cfg.money(snap['pnl'])} ({snap['pnl_pct']:+.2f}%)")
        lines.append(f"투자 중: {cfg.money(snap['invested'])} / 예산 {cfg.money(snap['budget'])}")
    for p in snap.get("positions", [])[:5]:
        lines.append(f" · {p['name']} {p['qty']}주 {p['pnl_pct']:+.1f}%")
    if ai["calls"]:
        lines.append(f"AI: {ai['calls']}회 · ${ai['usd']:.3f}")
    notify.send(cfg, "daily", "\n".join(lines))
    if market == "us" and base.us_enabled:            # 미국 마감이 그 거래일의 마지막 → 국내+미국 통합 요약도 한 번
        try:
            notify.send(cfg, "daily", combined_summary(base, now.date()))
        except Exception as e:  # noqa: BLE001
            log.warning("통합 마감 요약 실패: %s", e)
    return True


def combined_summary(base: Config, day) -> str:
    """국내 + 미국 하루 통합 요약 (원화). day = 거래일 (국내 날짜 = 뉴욕 날짜).
    미국 실현 손익은 거래 당시 환율(환차손익 포함), 보유 평가·예산은 최근 환율."""
    from datetime import datetime
    from . import ledger, pricing
    from .journal import KST
    kr, us = market_cfg(base, "kr"), market_cfg(base, "us")
    j, ju = Journal(kr.db_path), Journal(us.db_path)
    try:
        fx = float(j.get("fx_usdkrw") or 0) or 1400.0
    except ValueError:
        fx = 1400.0
    rk = ledger.period(j, kr.ledger, day, day, ledger.costs_of(kr))
    ru = ledger.period(ju, us.ledger, day, day, ledger.costs_of(us))
    won = lambda v: f"{v:+,.0f}원"                                     # noqa: E731
    lines = [f"📊 🌐 통합 마감 요약 ({day:%m/%d})",
             f"오늘 주문: 매수 {rk['buys'] + ru['buys']}건 · 매도 {rk['n'] + ru['n']}건",
             f"오늘 실현 손익: {won(rk['pnl_krw'] + ru['pnl_krw'])}"
             f" (국내 {won(rk['pnl_krw'])} · 미국 {us.money(ru['pnl'])} ≈ {won(ru['pnl_krw'])})"]
    sk, su = j.snapshot(kr.ledger) or {}, ju.snapshot(us.ledger) or {}
    if sk.get("budget") is not None or su.get("budget") is not None:
        tot = {k: float(sk.get(k) or 0) + float(su.get(k) or 0) * fx for k in ("budget", "pnl", "invested")}
        pct = tot["pnl"] / tot["budget"] * 100 if tot["budget"] else 0.0
        lines.append(f"봇 손익(누적): {won(tot['pnl'])} ({pct:+.2f}%)")
        lines.append(f"투자 중: {tot['invested']:,.0f}원 / 예산 {tot['budget']:,.0f}원 (환율 {fx:,.0f}원)")
    ai = pricing.ai_cost(j, kr.data_dir, None, datetime(day.year, day.month, day.day, tzinfo=KST))
    if ai["calls"]:
        lines.append(f"AI: {ai['calls']}회 · ${ai['usd']:.3f} (≈ {ai['usd'] * fx:,.0f}원)")
    return "\n".join(lines)


def weekly_summary(base: Config, at=None, force: bool = False) -> bool:
    """주간 리포트 — 그 주 마지막 장 마감 뒤 한 번 (미국을 쓰면 뉴욕 금요일 마감 뒤 = 토요일 아침, 아니면 국내 금요일 15:40).
    금요일이 휴장이면 주말에 보낸다. 그 주에 자동 실행이 한 번도 없었으면 보내지 않는다."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    from . import notify
    from .bot import CLOSE
    market = "us" if base.us_enabled else "kr"
    cfg = market_cfg(base, market)
    if not force and (not notify.enabled(cfg, "weekly") or not notify.channels(cfg.data_dir)):
        return False
    tzname, hh, mm = CLOSE[market]
    now = (at or datetime.now(ZoneInfo(tzname))).astimezone(ZoneInfo(tzname))
    wd = now.weekday()
    if not force and not (wd >= 5 or (wd == 4 and (now.hour, now.minute) >= (hh, mm + 10))):
        return False
    monday = now.date() - timedelta(days=wd)
    friday = monday + timedelta(days=4)
    j = Journal(market_cfg(base, "kr").db_path)
    key = "weekly_sent"
    if not force and j.get(key) == monday.isoformat():
        return False
    if not force:                                    # 직접 보내 보기(force)는 정기 발송을 막지 않게
        j.set(key, monday.isoformat())
    from .journal import KST
    since = datetime(monday.year, monday.month, monday.day, tzinfo=KST).isoformat(timespec="seconds")
    ran = j.db.execute("SELECT 1 FROM run_logs WHERE kind='trade' AND ts >= ? LIMIT 1", (since,)).fetchone()
    if not ran and not force:
        return False
    notify.send(cfg, "weekly", weekly_text(base, monday, friday), force=force)
    return True


def weekly_text(base: Config, monday, friday) -> str:
    """주간 리포트 본문 (국내 + 미국이면 미국도). 미국 실현 손익은 거래 당시 환율로 원화도 함께."""
    from . import bench, ledger, pricing
    mkts = ["kr"] + (["us"] if base.us_enabled else [])
    j0 = Journal(market_cfg(base, "kr").db_path)
    try:
        fx = float(j0.get("fx_usdkrw") or 0) or 1400.0
    except ValueError:
        fx = 1400.0
    won = lambda v: f"{v:+,.0f}원"                                     # noqa: E731
    lines = [f"🗓 주간 리포트 ({monday:%m/%d}~{friday:%m/%d})"]
    total_krw = 0.0
    for mk in mkts:
        c = market_cfg(base, mk)
        j = Journal(c.db_path)
        r = ledger.period(j, c.ledger, monday, friday, ledger.costs_of(c))
        total_krw += r["pnl_krw"]
        flag = "🇰🇷 국내" if mk == "kr" else "🇺🇸 미국"
        lines.append("")
        sg = lambda v: ("+" if v > 0 else "") + c.money(v)                         # noqa: E731
        pnl = won(r["pnl"]) if mk == "kr" else f"{sg(r['pnl'])} ≈ {won(r['pnl_krw'])}"
        lines.append(f"{flag} — 매수 {r['buys']}건 · 매도 {r['n']}건 · 실현 {pnl}")
        if r["n"]:
            ex = (f"최고 {r['best']['name']} {r['best']['pnl_pct']:+.1f}% · 최저 {r['worst']['name']} "
                  f"{r['worst']['pnl_pct']:+.1f}%") if r["n"] > 1 else f"{r['best']['name']} {r['best']['pnl_pct']:+.1f}%"
            lines.append(f"  승률 {r['win_rate']:.0f}% ({r['wins']}승 {r['n'] - r['wins']}패) · {ex}")
        snap = j.snapshot(c.ledger) or {}
        budget = float(snap.get("budget") or c.total_krw or 0)
        cmp_ = bench.line(j, mk, monday, friday, bench.bot_change(j, c.ledger, budget, monday, friday),
                          lambda v: ("+" if v > 0 else "") + c.money(v), budget)
        if cmp_:
            lines.append("  " + cmp_)
        if c.ai_hybrid:
            try:
                from . import scorecard
                sc = scorecard.summary(j, c.ledger)
                t, b = sc["taken"], sc["blocked"]
                if t["n"] or b["n"]:
                    avg = lambda g: "-" if g["avg"] is None else f"{g['avg']:+.2f}%"      # noqa: E731
                    lines.append(f"  AI 성적표(누적): 통과 {t['n']}건 평균 {avg(t)} · 막음 {b['n']}건 평균 {avg(b)}")
                    lines.append(f"  → {sc['verdict']}")
            except Exception as e:  # noqa: BLE001
                log.warning("주간 리포트 성적표 실패: %s", e)
    if len(mkts) > 1:
        lines += ["", f"🌐 합계 실현 손익: {won(total_krw)}"]
    ai = pricing.ai_cost(j0, market_cfg(base, "kr").data_dir, None, monday, friday)
    if ai["calls"]:
        lines.append(f"AI 비용: {ai['calls']}회 · ${ai['usd']:.3f} (≈ {ai['usd'] * fx:,.0f}원)")
    return "\n".join(lines)


def refresh_balance(cfg: Config, market: str = "kr") -> dict:
    """웹의 '잔고 새로고침' — 증권사에서 바로 조회해 스냅샷 갱신 (주문 없음)."""
    cfg = market_cfg(cfg, market)
    j = Journal(cfg.db_path).use_broker(broker_name(cfg), cfg.market, cfg.mode)
    broker = make_broker(cfg)
    bal = broker.balance()
    from . import ledger
    from .bot import account_view, own_holdings
    held = own_holdings(j, cfg, bal.holdings)
    if getattr(broker, "fx", 0):
        j.set("fx_usdkrw", str(broker.fx))
    st = ledger.status(j, cfg, held, bal.orderable_cash)
    snap = {
        **st,
        "positions": [{"code": c, "name": h.name, "qty": h.qty, "avg_price": h.avg_price,
                       "now_price": h.now_price, "pnl_pct": round(h.pnl_pct, 2)} for c, h in held.items()],
        "account": account_view(bal, held),
    }
    j.save_snapshot(cfg.ledger, snap)
    return snap


def run_scan(cfg: Config, source: str = "web") -> str:
    """자동 종목 다시 찾기 (잠금 + 실행 로그)."""
    from . import universe
    with run_lock(cfg, "종목 찾기"):
        j = Journal(cfg.db_path)
        try:
            r = universe.scan(cfg, make_broker(cfg), j)
            out = (f"🔎 자동 종목 검색: 1차 통과 {r['prefilter_pass']} → 시세 조회 {r['scanned']} → "
                   f"선정 {len(r['items'])}종목 ({r['seconds']}초)\n" +
                   "\n".join(f"  {i}. {it['name']}({it['code']}) {it['price']:,}원 · 전일 거래대금 {it['prev_value_eok']:,}억"
                             for i, it in enumerate(r["items"], 1)))
        except Exception as e:  # noqa: BLE001
            out = f"❌ 자동 종목 검색 실패: {e}"
        j.add_log(cfg.mode, "scan", source, out)
        return out
