"""일봉으로 기술지표 계산 (순수 파이썬 — pandas 불필요)."""
from __future__ import annotations

import math


def _sma(xs: list[float], n: int) -> float | None:
    return sum(xs[-n:]) / n if len(xs) >= n else None


def _rsi(closes: list[float], n: int = 14) -> float | None:
    if len(closes) < n + 1:
        return None
    gains = losses = 0.0
    for a, b in zip(closes[-n - 1:-1], closes[-n:]):
        d = b - a
        gains += max(d, 0)
        losses += max(-d, 0)
    if losses == 0:
        return 100.0
    rs = (gains / n) / (losses / n)
    return 100 - 100 / (1 + rs)


def _ret(closes: list[float], n: int) -> float | None:
    if len(closes) <= n or closes[-n - 1] == 0:
        return None
    return (closes[-1] / closes[-n - 1] - 1) * 100


def _vol(closes: list[float], n: int = 20) -> float | None:
    """일간 수익률 표준편차(%)."""
    if len(closes) < n + 1:
        return None
    rets = [(b / a - 1) for a, b in zip(closes[-n - 1:-1], closes[-n:]) if a]
    m = sum(rets) / len(rets)
    return math.sqrt(sum((r - m) ** 2 for r in rets) / len(rets)) * 100


def compute(daily: list[dict]) -> dict:
    closes = [float(r["close"]) for r in daily]
    vols = [float(r["volume"]) for r in daily]
    if not closes:
        return {}
    last = closes[-1]
    ma5, ma20, ma60 = _sma(closes, 5), _sma(closes, 20), _sma(closes, 60)
    vol20 = _sma(vols, 20)

    def r(x, nd=2):
        return None if x is None else round(x, nd)

    return {
        "close": last,
        "ma5": r(ma5, 0), "ma20": r(ma20, 0), "ma60": r(ma60, 0),
        "price_vs_ma20_pct": r((last / ma20 - 1) * 100) if ma20 else None,
        "ma5_above_ma20": (ma5 > ma20) if (ma5 and ma20) else None,
        "rsi14": r(_rsi(closes)),
        "ret_5d_pct": r(_ret(closes, 5)),
        "ret_20d_pct": r(_ret(closes, 20)),
        "ret_60d_pct": r(_ret(closes, 60)),
        "volatility_20d_pct": r(_vol(closes)),
        "volume_vs_20d_avg": r(vols[-1] / vol20) if vol20 else None,
        "days": len(closes),
    }
