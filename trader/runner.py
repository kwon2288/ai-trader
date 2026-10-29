"""매매 사이클 실행 공통부 — 명령줄(run.py)·타이머·웹이 모두 이걸 쓴다.

파일 잠금으로 동시에 두 번 돌지 않게 막는다 (타이머 실행 중 웹에서 누르는 경우 등).
"""
from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager

from .broker import BrokerError
from .config import Config
from .journal import Journal


class Busy(RuntimeError):
    pass


@contextmanager
def run_lock(cfg: Config):
    fh = open(cfg.data_dir / "run.lock", "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        raise Busy("다른 실행이 진행 중입니다. 잠시 후 다시 시도하세요.") from None
    try:
        yield
    finally:
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
    return cfg.mode != "live" or os.environ.get("I_UNDERSTAND_REAL_MONEY") == "yes"


def run_cycle(cfg: Config, analyze_only: bool = False, force: bool = False, source: str = "cli",
              market: str = "kr") -> str:
    """한 사이클 실행 후 결과 텍스트 반환. 결과는 일지(run_logs)에도 남긴다."""
    kind = "analyze" if analyze_only else "trade"
    with run_lock(cfg):
        j = Journal(cfg.db_path)
        try:
            cfg = market_cfg(cfg, market)
            if not analyze_only and not live_allowed(cfg):
                raise BrokerError("실계좌 모드는 .env 에 I_UNDERSTAND_REAL_MONEY=yes 가 있어야 실행됩니다")
            from .analyst import make_analyst
            from .bot import Bot
            bot = Bot(cfg, make_broker(cfg), make_analyst(cfg), j)
            out = bot.run(force=force, analyze_only=analyze_only)
        except Exception as e:  # noqa: BLE001
            out = f"❌ 실행 오류: {e}"
        j.add_log(cfg.ledger, kind, source, out)
        return out


def refresh_balance(cfg: Config, market: str = "kr") -> dict:
    """웹의 '잔고 새로고침' — 증권사에서 바로 조회해 스냅샷 갱신 (주문 없음)."""
    cfg = market_cfg(cfg, market)
    j = Journal(cfg.db_path)
    broker = make_broker(cfg)
    bal = broker.balance()
    bot_codes = j.bot_codes(cfg.market)
    held = {c: h for c, h in bal.holdings.items() if c in bot_codes}
    cash = float(j.get(f"bot_cash_{cfg.ledger}") or cfg.total_krw)
    cash = round(cash, 2) if cfg.currency == "USD" else int(cash)
    equity = round(cash + sum(h.value for h in held.values()), 2)
    snap = {
        "equity": equity, "cash": cash,
        "pnl_pct": round((equity / cfg.total_krw - 1) * 100, 2),
        "account_cash": bal.orderable_cash if bal.orderable_cash != float("inf") else None,
        "positions": [{"code": c, "name": h.name, "qty": h.qty, "avg_price": h.avg_price,
                       "now_price": h.now_price, "pnl_pct": round(h.pnl_pct, 2)} for c, h in held.items()],
    }
    j.save_snapshot(cfg.ledger, snap)
    return snap


def run_scan(cfg: Config, source: str = "web") -> str:
    """자동 종목 다시 찾기 (잠금 + 실행 로그)."""
    from . import universe
    with run_lock(cfg):
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
