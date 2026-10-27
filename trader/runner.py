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


def make_broker(cfg: Config):
    broker = os.environ.get("BROKER", "nh").strip().lower()
    if broker == "kis":
        from .kis import KISBroker
        return KISBroker(cfg.mode, cfg.data_dir)
    if broker != "nh":
        raise BrokerError(f"BROKER 는 nh 또는 kis 여야 합니다 (현재: {broker})")
    try:
        from .broker import NHBroker
        return NHBroker(cfg.mode)
    except ImportError:
        raise BrokerError("nhplug 패키지가 없습니다 → .venv/bin/pip install -r requirements.txt") from None


def live_allowed(cfg: Config) -> bool:
    return cfg.mode != "live" or os.environ.get("I_UNDERSTAND_REAL_MONEY") == "yes"


def run_cycle(cfg: Config, analyze_only: bool = False, force: bool = False, source: str = "cli") -> str:
    """한 사이클 실행 후 결과 텍스트 반환. 결과는 일지(run_logs)에도 남긴다."""
    kind = "analyze" if analyze_only else "trade"
    with run_lock(cfg):
        j = Journal(cfg.db_path)
        try:
            if not analyze_only and not live_allowed(cfg):
                raise BrokerError("실계좌 모드는 .env 에 I_UNDERSTAND_REAL_MONEY=yes 가 있어야 실행됩니다")
            from .analyst import make_analyst
            from .bot import Bot
            bot = Bot(cfg, make_broker(cfg), make_analyst(cfg), j)
            out = bot.run(force=force, analyze_only=analyze_only)
        except Exception as e:  # noqa: BLE001
            out = f"❌ 실행 오류: {e}"
        j.add_log(cfg.mode, kind, source, out)
        return out


def refresh_balance(cfg: Config) -> dict:
    """웹의 '잔고 새로고침' — 증권사에서 바로 조회해 스냅샷 갱신 (주문 없음)."""
    j = Journal(cfg.db_path)
    broker = make_broker(cfg)
    bal = broker.balance()
    bot_codes = j.bot_codes()
    held = {c: h for c, h in bal.holdings.items() if c in bot_codes}
    cash = int(j.get(f"bot_cash_{cfg.mode}") or cfg.total_krw)
    equity = cash + sum(h.value for h in held.values())
    snap = {
        "equity": equity, "cash": cash,
        "pnl_pct": round((equity / cfg.total_krw - 1) * 100, 2),
        "account_cash": bal.orderable_cash,
        "positions": [{"code": c, "name": h.name, "qty": h.qty, "avg_price": h.avg_price,
                       "now_price": h.now_price, "pnl_pct": round(h.pnl_pct, 2)} for c, h in held.items()],
    }
    j.save_snapshot(cfg.mode, snap)
    return snap
