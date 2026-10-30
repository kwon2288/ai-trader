"""규칙 기반 신호 — AI 없이 지표만으로 '볼 만한 종목'을 고른다.

쓰임새
  - 하이브리드(ai.hybrid = true): 신호가 난 종목만 AI 에게 보내 최종 확인 → 신호 없으면 AI 호출 생략(토큰 0)
  - 규칙만(provider = "rules"): 신호를 그대로 매매 판단으로 사용 (AI 비용 0원)

전략: 추세 추종 + 과매도 반등(보조)
  매수(보유하지 않은 종목)
    · 추세: 5일선 > 20일선, 현재가가 20일선 위 0~8% (너무 멀리 달아나지 않음),
            20일 수익률 > 0, 60일선 위(60일 데이터가 있을 때), RSI ≤ 68 (과열 아님)
    · 반등: RSI ≤ 32 이고 오늘 상승 중
  매도(봇 보유 종목)
    · 추세 이탈: 5일선 < 20일선 이고 현재가가 20일선보다 1% 이상 아래
    · 과열: RSI ≥ 75
    · 60일선 3% 이상 하회
  손절·익절은 이와 별개로 bot.py 가 항상 먼저 처리한다.
"""
from __future__ import annotations

# 조정하고 싶으면 이 값들만 바꾸면 된다
TREND_MAX_ABOVE_MA20_PCT = 8.0
TREND_MAX_RSI = 68.0
BOUNCE_MAX_RSI = 32.0
SELL_BELOW_MA20_PCT = -1.0      # 20일선을 이만큼 밑돌아야 '이탈' (횡보 잡음 방지)
SELL_RSI = 75.0
SELL_BELOW_MA60_PCT = -3.0


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def buy_signal(c: dict) -> dict | None:
    if c.get("held") or c.get("warnings") or not c.get("affordable", True):
        return None
    close, ma20, ma60 = _num(c.get("close")), _num(c.get("ma20")), _num(c.get("ma60"))
    rsi, vs20 = _num(c.get("rsi14")), _num(c.get("price_vs_ma20_pct"))
    r20, chg = _num(c.get("ret_20d_pct")), _num(c.get("today_change_pct"))
    if None in (close, ma20, rsi, vs20, r20):
        return None                                          # 데이터 부족(신규 상장 등)
    if (c.get("ma5_above_ma20") is True and 0 <= vs20 <= TREND_MAX_ABOVE_MA20_PCT
            and r20 > 0 and rsi <= TREND_MAX_RSI and (ma60 is None or close >= ma60)):
        strength = min(0.2, r20 / 50)                        # 20일 +10% → +0.2
        return {"type": "BUY_TREND", "strength": round(0.6 + strength, 2),
                "reason": f"상승 추세: 5일선>20일선, 20일선 위 {vs20:+.1f}%, 20일 {r20:+.1f}%, RSI {rsi:.0f}"}
    if rsi <= BOUNCE_MAX_RSI and chg is not None and chg > 0:
        return {"type": "BUY_BOUNCE", "strength": 0.6,
                "reason": f"과매도 반등: RSI {rsi:.0f}, 오늘 {chg:+.1f}%"}
    return None


def sell_signal(c: dict) -> dict | None:
    if not c.get("held"):
        return None
    close, ma20, ma60 = _num(c.get("close")), _num(c.get("ma20")), _num(c.get("ma60"))
    rsi = _num(c.get("rsi14"))
    if close and ma20 and c.get("ma5_above_ma20") is False and (close / ma20 - 1) * 100 <= SELL_BELOW_MA20_PCT:
        return {"type": "SELL_TREND_BREAK", "strength": 0.7,
                "reason": f"추세 이탈: 5일선<20일선, 20일선 아래 {(close / ma20 - 1) * 100:+.1f}%"}
    if rsi is not None and rsi >= SELL_RSI:
        return {"type": "SELL_OVERHEAT", "strength": 0.65, "reason": f"과열: RSI {rsi:.0f}"}
    if close and ma60 and (close / ma60 - 1) * 100 <= SELL_BELOW_MA60_PCT:
        return {"type": "SELL_BELOW_MA60", "strength": 0.7,
                "reason": f"60일선 이탈 {(close / ma60 - 1) * 100:+.1f}%"}
    return None


# ── 단기 매매용 전략들 ([strategy] signal 로 고름) ─────────────────────
DIP_MAX_RSI = 35.0          # 눌림목: 이 아래로 빠졌다가
DIP_EXIT_RSI = 60.0         #        여기까지 반등하면 판다
BREAKOUT_MIN_VOL = 1.5      # 돌파: 거래량이 20일 평균의 1.5배 이상
VOLUME_MIN = 2.0            # 거래량 급증: 20일 평균의 2배 이상
VOLUME_CHG = (1.0, 8.0)     #   그날 +1 ~ +8% 오른 날만 (너무 급등한 날은 거름)


def _ok(c):
    return not (c.get("held") or c.get("warnings") or not c.get("affordable", True))


def dip_buy(c):
    """눌림목(과매도 반등): 60일선 위 상승 흐름에서 RSI가 35 아래로 빠진 뒤 오늘 반등."""
    if not _ok(c):
        return None
    close, ma60, rsi, chg = (_num(c.get(k)) for k in ("close", "ma60", "rsi14", "today_change_pct"))
    if None in (close, rsi, chg) or (ma60 is not None and close < ma60 * 0.97):
        return None
    if rsi <= DIP_MAX_RSI and chg > 0:
        return {"type": "BUY_DIP", "strength": round(0.6 + min(0.2, (DIP_MAX_RSI - rsi) / 50), 2),
                "reason": f"눌림목 반등: RSI {rsi:.0f}, 오늘 {chg:+.1f}%"}
    return None


def dip_sell(c):
    if not c.get("held"):
        return None
    rsi, close, ma60 = _num(c.get("rsi14")), _num(c.get("close")), _num(c.get("ma60"))
    if rsi is not None and rsi >= DIP_EXIT_RSI:
        return {"type": "SELL_REBOUND", "strength": 0.7, "reason": f"반등 완료: RSI {rsi:.0f}"}
    if close and ma60 and close < ma60 * 0.93:
        return {"type": "SELL_BELOW_MA60", "strength": 0.7, "reason": f"60일선 {(close / ma60 - 1) * 100:+.1f}% — 흐름 꺾임"}
    return None


def breakout_buy(c):
    """돌파: 오늘 종가가 어제까지 20일 최고가를 넘고 거래량이 붙음."""
    if not _ok(c):
        return None
    close, hi, vol, rsi = (_num(c.get(k)) for k in ("close", "high_20d_prev", "volume_vs_20d_avg", "rsi14"))
    if None in (close, hi, vol) or not hi:
        return None
    if close > hi and vol >= BREAKOUT_MIN_VOL and (rsi is None or rsi <= 80):
        return {"type": "BUY_BREAKOUT", "strength": round(0.6 + min(0.2, (vol - 1) / 10), 2),
                "reason": f"20일 신고가 돌파: {(close / hi - 1) * 100:+.1f}%, 거래량 {vol:.1f}배"}
    return None


def breakout_sell(c):
    if not c.get("held"):
        return None
    close, lo = _num(c.get("close")), _num(c.get("low_10d_prev"))
    if close and lo and close < lo:
        return {"type": "SELL_CHANNEL", "strength": 0.7, "reason": "10일 최저가 이탈"}
    return None


def volume_buy(c):
    """거래량 급증: 20일 평균 2배 이상 거래 + 상승 마감 + 20일선 위."""
    if not _ok(c):
        return None
    close, ma20, vol, chg = (_num(c.get(k)) for k in ("close", "ma20", "volume_vs_20d_avg", "today_change_pct"))
    if None in (close, ma20, vol, chg):
        return None
    if vol >= VOLUME_MIN and VOLUME_CHG[0] <= chg <= VOLUME_CHG[1] and close >= ma20:
        return {"type": "BUY_VOLUME", "strength": round(0.6 + min(0.2, (vol - 2) / 10), 2),
                "reason": f"거래량 급증 {vol:.1f}배, 오늘 {chg:+.1f}%"}
    return None


def volume_sell(c):
    if not c.get("held"):
        return None
    close, ma5, rsi = _num(c.get("close")), _num(c.get("ma5")), _num(c.get("rsi14"))
    if close and ma5 and close < ma5 * 0.99:
        return {"type": "SELL_MA5", "strength": 0.7, "reason": f"5일선 이탈 {(close / ma5 - 1) * 100:+.1f}%"}
    if rsi is not None and rsi >= SELL_RSI:
        return {"type": "SELL_OVERHEAT", "strength": 0.65, "reason": f"과열: RSI {rsi:.0f}"}
    return None


MODES = {
    "trend": ("추세 추종", buy_signal, sell_signal),
    "dip": ("눌림목 반등", dip_buy, dip_sell),
    "breakout": ("신고가 돌파", breakout_buy, breakout_sell),
    "volume": ("거래량 급증", volume_buy, volume_sell),
}


def label(mode: str) -> str:
    return MODES.get(mode, MODES["trend"])[0]


def evaluate(candidates: list[dict], can_buy: bool = True, mode: str = "trend") -> dict[str, dict]:
    """종목코드 → 신호. 살 자리가 없으면(can_buy=False) 매수 신호는 세지 않는다."""
    _, buy_fn, sell_fn = MODES.get(mode, MODES["trend"])
    out = {}
    for c in candidates:
        s = sell_fn(c) or (buy_fn(c) if can_buy else None)
        if s:
            out[c["code"]] = s
    return out


class RulesAnalyst:
    """AI 없이 신호를 그대로 판단으로 쓰는 분석기 (provider = "rules")."""
    name = "rules"
    label = "규칙만(AI 없음)"

    def decide(self, ctx: dict) -> dict:
        pf = ctx.get("portfolio", {})
        rules = ctx.get("rules", {})
        slots = rules.get("max_positions", 1) - len(pf.get("positions", []))
        sig = evaluate(ctx.get("candidates", []), can_buy=slots > 0, mode=rules.get("signal_mode", "trend"))
        decisions = []
        for code, s in sig.items():
            if s["type"].startswith("SELL"):
                decisions.append({"code": code, "action": "SELL", "sell_ratio": 1.0, "amount_krw": 0,
                                  "confidence": s["strength"], "reason": f"[규칙] {s['reason']}"})
            else:
                decisions.append({"code": code, "action": "BUY", "sell_ratio": 0,
                                  "amount_krw": rules.get("max_per_position", 0),
                                  "confidence": s["strength"], "reason": f"[규칙] {s['reason']}"})
        buys = sum(d["action"] == "BUY" for d in decisions)
        sells = len(decisions) - buys
        return {"market_view": f"규칙 신호: 매수 {buys} · 매도 {sells} (후보 {len(ctx.get('candidates', []))}종목)",
                "decisions": decisions, "_provider": self.label, "_usage": None}
