"""분석 대상 종목 — 핵심(고정) + 자동 선정(매일 갱신).

자동 선정 2단계
  1) NH 종목마스터(전 종목, 매일 갱신 · API 호출 없음)로 1차 거르기
     가격 범위, 최소 시가총액, 관리·정지·투자유의·단기과열·투자경고·정리매매·불성실공시 제외,
     우선주·스팩·ETN 제외, ETF 는 옵션(레버리지·인버스·채권·금리형은 항상 제외)
  2) 남은 종목 중 시가총액 상위 N개만 현재가 API 로 조회 → 전일 거래대금 순으로 선택

결과는 일지(state)에 저장되고, 그날 첫 실행 때 한 번만 검색한다.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime

from .broker import KST, _int

log = logging.getLogger(__name__)

STATE_KEY = "auto_universe"
ETF_EXCLUDE = re.compile(
    r"레버리지|인버스|2X|3X|선물|채권|국채|국고채|회사채|금리|CD|KOFR|SOFR|머니마켓|단기|통안|MMF|커버드콜",
    re.I)


# ── 1차: 종목마스터 ──────────────────────────────────────
def load_stock_master() -> list[dict]:
    from nhplug.instruments import load_master
    rows = load_master("m_new_stock")
    if hasattr(rows, "to_dict"):          # pandas 가 깔려 있으면 DataFrame 으로 온다
        rows = rows.to_dict("records")
    return list(rows)


def prefilter(rows: list[dict], *, min_price: int, max_price: int, min_cap_eok: int,
              include_etf: bool) -> list[dict]:
    out = []
    for r in rows:
        code = str(r.get("sCode", "")).strip()
        name = str(r.get("name") or r.get("sKorName") or "").strip().lstrip("*#").strip()
        market = str(r.get("sMarket", "")).strip()
        is_etf = market == "1" and str(r.get("gVenture", "")).strip() == "8"
        price = _int(r.get("sPrePrice"))
        cap = _int(r.get("prdy_avls"))
        if market not in ("1", "4") or len(code) != 6:
            continue                                   # ETN 등 제외
        if code[-1] != "0" or "스팩" in name:
            continue                                   # 우선주·스팩
        if is_etf and (not include_etf or ETF_EXCLUDE.search(name)):
            continue
        if any(str(r.get(k, "")).strip() == "Y" for k in
               ("eUnder", "eStop", "eWarn", "eGongsi", "invt_epmd_issu_yn", "sltr_yn")):
            continue
        if str(r.get("short_over_issu_cls_code", "0")).strip() not in ("", "0"):
            continue
        if str(r.get("alert_gb", "0")).strip() not in ("", "0"):
            continue
        if not (min_price <= price <= max_price) or cap < min_cap_eok:
            continue
        out.append({"code": code, "name": name, "prev_close": price, "cap_eok": cap, "etf": is_etf})
    out.sort(key=lambda x: -x["cap_eok"])
    return out


# ── 2차: 현재가로 순위 ───────────────────────────────────
def scan(cfg, broker, journal, progress=None) -> dict:
    """자동 종목 검색 후 저장. 실패하면 예외 (호출 측에서 직전 목록 유지)."""
    if not hasattr(broker, "scan_quote"):
        raise RuntimeError("자동 종목 선정은 NH 나무증권(BROKER=nh)에서만 지원합니다")
    started = datetime.now(KST)
    pool = prefilter(load_stock_master(), min_price=cfg.auto_min_price,
                     max_price=cfg.max_per_position_krw, min_cap_eok=cfg.auto_min_cap_eok,
                     include_etf=cfg.auto_include_etf)
    total_pass = len(pool)
    pool = [p for p in pool if p["code"] not in set(cfg.symbols)][:cfg.auto_scan_pool]

    ranked, fails = [], 0
    for i, p in enumerate(pool):
        if progress and i % 25 == 0:
            progress(i, len(pool))
        try:
            s = broker.scan_quote(p["code"])
        except Exception as e:  # noqa: BLE001
            fails += 1
            log.info("scan %s 실패: %s", p["code"], e)
            continue
        if s["warnings"] or not (cfg.auto_min_price <= s["price"] <= cfg.max_per_position_krw):
            continue
        ranked.append({**p, "price": s["price"], "prev_value_eok": round(s["prev_value"] / 1e8, 1)})
    if pool and fails > len(pool) // 2:
        raise RuntimeError(f"시세 조회 실패가 너무 많습니다 ({fails}/{len(pool)})")

    ranked.sort(key=lambda x: -x["prev_value_eok"])
    picks = ranked[:cfg.auto_count]
    result = {
        "date": started.strftime("%Y-%m-%d"), "ts": started.isoformat(timespec="seconds"),
        "seconds": round((datetime.now(KST) - started).total_seconds()),
        "prefilter_pass": total_pass, "scanned": len(pool), "items": picks,
    }
    journal.set(STATE_KEY, json.dumps(result, ensure_ascii=False))
    return result


def saved(journal) -> dict | None:
    v = journal.get(STATE_KEY)
    try:
        return json.loads(v) if v else None
    except ValueError:
        return None


def ensure_today(cfg, broker, journal, say=None) -> list[dict]:
    """오늘 검색한 목록이 없으면 검색. 실패하면 직전 목록(있으면)을 쓴다."""
    if cfg.auto_count <= 0:
        return []
    cur = saved(journal)
    today = datetime.now(KST).strftime("%Y-%m-%d")
    if cur and cur.get("date") == today:
        return cur["items"]
    try:
        if say:
            say(f"🔎 오늘의 자동 종목 검색 중… (시총 상위 최대 {cfg.auto_scan_pool}개 조회, 1분 정도)")
        cur = scan(cfg, broker, journal)
        if say:
            names = ", ".join(i["name"] for i in cur["items"])
            say(f"🔎 자동 선정 {len(cur['items'])}종목 ({cur['seconds']}초): {names}")
        return cur["items"]
    except Exception as e:  # noqa: BLE001
        if say:
            say(f"⚠ 자동 종목 검색 실패 — {'직전 목록 사용' if cur else '핵심 종목만 분석'}: {e}")
        return (cur or {}).get("items", [])
