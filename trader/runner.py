"""매매 사이클 실행 공통부 — 명령줄(run.py)·타이머·웹이 모두 이걸 쓴다.

파일 잠금으로 동시에 두 번 돌지 않게 막는다 (타이머 실행 중 웹에서 누르는 경우 등).
"""
from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager

from .broker import BrokerError
from .config import Config
from .journal import KST, Journal


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
            bot = Bot(cfg, make_broker(cfg), analyst, j)
            out = bot.run(force=force, analyze_only=analyze_only)
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
    snap = j.snapshot(cfg.ledger) or {}
    today = ledger.period(j, cfg.ledger, now.date(), now.date(), ledger.costs_of(cfg))
    ai = pricing.ai_cost(j, cfg.data_dir, cfg.ledger, start.astimezone(KST).date())
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
    return True


def refresh_balance(cfg: Config, market: str = "kr") -> dict:
    """웹의 '잔고 새로고침' — 증권사에서 바로 조회해 스냅샷 갱신 (주문 없음)."""
    cfg = market_cfg(cfg, market)
    j = Journal(cfg.db_path)
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
