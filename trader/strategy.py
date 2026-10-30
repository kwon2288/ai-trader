"""전략 옵션 — 시장 필터 · 코어-위성.

config [strategy]
  market_filter = false   # 지수(국내 KODEX 200, 미국 SPY)가 200일선 아래면 새로 사지 않음 (매도·손절은 그대로)
  core_pct = 0            # 예산 중 이 비율(%)은 코어 ETF 를 사서 계속 보유 (봇 매매·손절 대상 아님)
  core_kr = "069500"      # 국내 코어 ETF (KODEX 200)
  core_us = "SPYM"        # 미국 코어 ETF (S&P 500)
  signal = "trend"        # 매매 신호: trend(추세 추종) · dip(눌림목 반등) · breakout(신고가 돌파) · volume(거래량 급증)
  max_hold_days = 0       # 이 날수가 지나면 수익·손실과 상관없이 정리 (단기 매매용, 0 = 끔)
"""
from __future__ import annotations

BENCH = {"kr": ("069500", "KODEX 200 (코스피200)"), "us": ("SPY", "SPY (S&P 500)")}
CORE_DEFAULT = {"kr": "069500", "us": "SPYM"}
MA_LONG = 200


def settings(cfg) -> dict:
    s = getattr(cfg, "strategy", None) or {}
    try:
        core = max(0, min(90, int(float(s.get("core_pct", 0)))))
    except (TypeError, ValueError):
        core = 0
    try:
        max_hold = max(0, min(60, int(float(s.get("max_hold_days", 0)))))
    except (TypeError, ValueError):
        max_hold = 0
    sig = str(s.get("signal", "trend")).strip()
    from .signals import MODES
    mk = getattr(cfg, "market", "kr")
    return {"market_filter": bool(s.get("market_filter", False)), "core_pct": core,
            "signal": sig if sig in MODES else "trend", "max_hold_days": max_hold,
            "core_code": str(s.get(f"core_{mk}") or CORE_DEFAULT[mk]).strip().upper() if mk == "us"
            else str(s.get(f"core_{mk}") or CORE_DEFAULT[mk]).strip()}


def regime_from_bars(bars: list[dict]) -> tuple[bool | None, str]:
    """지수 일봉으로 시장 상태 판단. (매수 허용?, 설명) — 데이터가 모자라면 (None, 사유)."""
    closes = [b["close"] for b in bars if b.get("close")]
    if len(closes) < MA_LONG:
        return None, f"지수 데이터 {len(closes)}일 — 200일선 계산 불가"
    ma = sum(closes[-MA_LONG:]) / MA_LONG
    ok = closes[-1] >= ma
    return ok, f"지수 {closes[-1]:,.2f} {'≥' if ok else '<'} 200일선 {ma:,.2f}"


def regime(broker, cfg) -> tuple[bool | None, str]:
    from pathlib import Path
    from . import backtest
    code = BENCH[cfg.market][0]
    try:
        bars = backtest.load_bars(broker, code, MA_LONG + 30, Path(cfg.data_dir) / "bars" / cfg.market)
    except Exception as e:  # noqa: BLE001
        return None, f"지수 조회 실패: {e}"
    return regime_from_bars(bars)
