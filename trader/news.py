"""뉴스·공시 — AI 에게 후보 종목의 최근 소식을 함께 넘긴다 (설정에서 켜고 끔, 기본 꺼짐).

- 뉴스: Google 뉴스 RSS 검색 (키 없음). 종목명으로 최근 3일 기사 제목 몇 개.
- 공시(국내): DART OpenAPI — .env 의 DART_API_KEY 가 있을 때만 (opendart.fss.or.kr 에서 무료 발급). 최근 7일.
- 같은 종목은 1시간 동안 캐시. 실패해도 매매는 그대로 진행 (소식 없이).
- AI 에는 '제목·날짜'만 보낸다 (본문 X) → 종목당 토큰 수십~백여 개 증가.
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote

import requests

from .journal import KST

log = logging.getLogger("news")
TTL = 3600
MAX_NEWS, MAX_DISC = 3, 3
UA = {"User-Agent": "Mozilla/5.0 (ai-trader)"}


def _cache(data_dir: Path) -> tuple[Path, dict]:
    f = Path(data_dir) / "news_cache.json"
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        d = {}
    return f, d


def google_news(query: str, market: str, n: int = MAX_NEWS) -> list[dict]:
    loc = "hl=ko&gl=KR&ceid=KR:ko" if market == "kr" else "hl=en-US&gl=US&ceid=US:en"
    url = f"https://news.google.com/rss/search?q={quote(query + ' when:3d')}&{loc}"
    r = requests.get(url, headers=UA, timeout=8)
    r.raise_for_status()
    out = []
    for it in ET.fromstring(r.content).iter("item"):
        title = (it.findtext("title") or "").strip()
        src = (it.findtext("source") or "").strip()
        if src and title.endswith(" - " + src):
            title = title[: -len(src) - 3]
        try:
            d = parsedate_to_datetime(it.findtext("pubDate") or "").astimezone(KST).strftime("%m-%d")
        except (TypeError, ValueError):
            d = ""
        if title:
            out.append({"date": d, "title": title[:120], "source": src[:30]})
        if len(out) >= n:
            break
    return out


# ── DART ──
def _corp_map(data_dir: Path, key: str) -> dict[str, str]:
    f = Path(data_dir) / "dart_corp.json"
    if f.exists() and time.time() - f.stat().st_mtime < 30 * 86400:
        try:
            return json.loads(f.read_text())
        except ValueError:
            pass
    r = requests.get("https://opendart.fss.or.kr/api/corpCode.xml", params={"crtfc_key": key}, timeout=30)
    r.raise_for_status()
    z = zipfile.ZipFile(io.BytesIO(r.content))
    root = ET.fromstring(z.read(z.namelist()[0]))
    m = {}
    for el in root.iter("list"):
        sc = (el.findtext("stock_code") or "").strip()
        if sc:
            m[sc] = (el.findtext("corp_code") or "").strip()
    f.write_text(json.dumps(m))
    return m


def dart(code: str, data_dir: Path, n: int = MAX_DISC) -> list[dict]:
    key = os.environ.get("DART_API_KEY", "").strip()
    if not key or not re.fullmatch(r"\d{6}", code):
        return []
    corp = _corp_map(data_dir, key).get(code)
    if not corp:
        return []                                   # ETF 등은 공시 대상 아님
    today = datetime.now(KST).date()
    r = requests.get("https://opendart.fss.or.kr/api/list.json", timeout=10, params={
        "crtfc_key": key, "corp_code": corp, "bgn_de": (today - timedelta(days=7)).strftime("%Y%m%d"),
        "end_de": today.strftime("%Y%m%d"), "page_count": str(n)})
    d = r.json()
    if d.get("status") not in ("000", "013"):        # 013 = 조회 결과 없음
        raise RuntimeError(f"DART {d.get('status')}: {d.get('message')}")
    return [{"date": f"{x['rcept_dt'][4:6]}-{x['rcept_dt'][6:]}", "title": x.get("report_nm", "").strip()}
            for x in (d.get("list") or [])[:n]]


def enrich(candidates: list[dict], market: str, data_dir: Path, say=None) -> int:
    """후보에 news / disclosures 를 붙인다. 반환: 소식이 붙은 종목 수."""
    f, cache = _cache(data_dir)
    now, n, errs = time.time(), 0, set()
    for c in candidates:
        key = f"{market}:{c['code']}"
        hit = cache.get(key)
        if not hit or now - hit.get("t", 0) > TTL:
            hit = {"t": now, "news": [], "disc": []}
            q = c.get("name") or c["code"]
            if market == "us" and c["code"] not in q:
                q = f"{q} {c['code']} stock"
            try:
                hit["news"] = google_news(q, market)
            except Exception as e:  # noqa: BLE001
                errs.add(f"뉴스: {e}")
            if market == "kr":
                try:
                    hit["disc"] = dart(c["code"], data_dir)
                except Exception as e:  # noqa: BLE001
                    errs.add(f"공시: {e}")
            cache[key] = hit
        if hit["news"]:
            c["news"] = [f"{x['date']} {x['title']}" for x in hit["news"]]
        if hit["disc"]:
            c["disclosures"] = [f"{x['date']} {x['title']}" for x in hit["disc"]]
        n += int(bool(hit["news"] or hit["disc"]))
    cache = {k: v for k, v in cache.items() if now - v.get("t", 0) < 86400}
    try:
        f.write_text(json.dumps(cache, ensure_ascii=False))
    except OSError:
        pass
    if say and errs:
        say("⚠ 소식 조회 일부 실패 (" + "; ".join(sorted(errs))[:200] + ")")
    return n
