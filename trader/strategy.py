"""전략 옵션 — 시장 필터 · 코어-위성.

config [strategy]
  market_filter = false   # 지수(국내 KODEX 200, 미국 SPY)가 200일선 아래면 새로 사지 않음 (매도·손절은 그대로)
  core_pct = 0            # 예산 중 이 비율(%)은 코어 ETF 를 사서 계속 보유 (봇 매매·손절 대상 아님)
  core_kr = "069500"      # 국내 코어 ETF (KODEX 200)
  core_us = "SPYM"        # 미국 코어 ETF (S&P 500)
  signal = "trend"        # 매매 신호: trend(추세 추종) · dip(눌림목 반등) · breakout(신고가 돌파) · volume(거래량 급증)
  max_hold_days = 0       # 이 날수가 지나면 수익·손실과 상관없이 정리 (단기 매매용, 0 = 끔)
  rule_sell = "direct"    # 규칙 매도 신호: direct(AI 확인 없이 바로 매도) · ai(AI 가 확인)
  guard = true            # 보호 규칙 묶음 (시장별): 추격 매수 차단 · 돌파 실패 조기 정리 · 이익 보호
  chase_max_ret5 = 12     #   최근 5일 상승률이 이 % 를 넘은 종목은 새로 사지 않음 (0 = 끔)
  protect_at_pct = 5      #   수익이 한 번 이 % 를 넘으면 손절선을 본전으로 올림 (0 = 끔)
  trail_pct = 5           #   그 뒤로는 고점 대비 이 % 내려오면 정리 (0 = 본전만 지킴)
  late_buy = true         # 신고가 돌파 매수는 그날 마지막 정기 실행에서만 (장중 가짜 돌파 방지, 시장별)

config [strategy.kr] / [strategy.us] — 시장별로 따로 (없으면 위 [strategy] 값)
  signal · max_hold_days · market_filter · stop_loss_pct · take_profit_pct · min_hold_days
"""
from __future__ import annotations

BENCH = {"kr": ("069500", "KODEX 200 (코스피200)"), "us": ("SPY", "SPY (S&P 500)")}
CORE_DEFAULT = {"kr": "069500", "us": "SPYM"}
MA_LONG = 200


MARKET_KEYS = ("signal", "max_hold_days", "market_filter", "stop_loss_pct", "take_profit_pct", "min_hold_days",
               "guard", "late_buy")
GUARD_DEFAULT = {"chase_max_ret5": 12.0, "protect_at_pct": 5.0, "trail_pct": 5.0}
FAIL_EXIT_BUFFER_PCT = 1.0      # 돌파선보다 이만큼 더 내려와야 '돌파 실패' (잡음 방지)
BREAKEVEN_PAD_PCT = 0.5         # 본전 = 평단 + 수수료·세금 몫


def merged(raw: dict, market: str) -> dict:
    """[strategy] 공통 값 위에 [strategy.<시장>] 값을 덮어쓴 것."""
    raw = raw or {}
    base = {k: v for k, v in raw.items() if not isinstance(v, dict)}
    over = raw.get(market) if isinstance(raw.get(market), dict) else {}
    return {**base, **over}


def settings(cfg) -> dict:
    mk = getattr(cfg, "market", "kr")
    s = merged(getattr(cfg, "strategy", None) or {}, mk)
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
    rule_sell = str(s.get("rule_sell", "direct")).strip().lower()
    nums = {}
    for k, d in GUARD_DEFAULT.items():
        try:
            nums[k] = max(0.0, min(100.0, float(s.get(k, d))))
        except (TypeError, ValueError):
            nums[k] = d
    return {"market_filter": bool(s.get("market_filter", False)), "core_pct": core,
            "guard": bool(s.get("guard", True)), "late_buy": bool(s.get("late_buy", True)), **nums,
            "signal": sig if sig in MODES else "trend", "max_hold_days": max_hold,
            "rule_sell": rule_sell if rule_sell in ("direct", "ai") else "direct",
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


def guard_opts(sg: dict, on: bool | None = None) -> dict | None:
    """보호 규칙 값 (꺼져 있으면 None). on 을 주면 설정의 guard 대신 그 값으로 (백테스트 비교용)."""
    if not (sg.get("guard", True) if on is None else on):
        return None
    return {"chase_max_ret5": sg.get("chase_max_ret5", GUARD_DEFAULT["chase_max_ret5"]), "fail_exit": True,
            "protect_at": sg.get("protect_at_pct", GUARD_DEFAULT["protect_at_pct"]),
            "trail": sg.get("trail_pct", GUARD_DEFAULT["trail_pct"])}


def protect_floor(avg: float, peak: float, opts: dict | None) -> float | None:
    """이익 보호선: 수익이 protect_at % 를 넘은 적이 있으면 max(본전, 고점 − trail %). 아직 아니면 None."""
    if not opts or not opts.get("protect_at") or not avg or not peak:
        return None
    if (peak / avg - 1) * 100 < opts["protect_at"]:
        return None
    floor = avg * (1 + BREAKEVEN_PAD_PCT / 100)
    if opts.get("trail"):
        floor = max(floor, peak * (1 - opts["trail"] / 100))
    return floor
