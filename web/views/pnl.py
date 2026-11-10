"""웹 화면 — 손익 · AI 사용량."""

from datetime import datetime, timedelta

from flask import flash, redirect, render_template, request, session, url_for

from trader.journal import KST, Journal  # noqa: E402
from web import settings_io  # noqa: E402
from web import app as webapp  # noqa: E402,F401
from web.app import app, login_required  # noqa: E402,F401
from trader.strategy import BENCH  # noqa: E402
from web.views.backtest import lines_svg  # noqa: E402


PERIODS = [("today", "오늘"), ("week", "이번 주"), ("month", "이번 달"), ("3m", "최근 3개월"),
           ("year", "올해"), ("all", "전체"), ("custom", "기간 지정")]


def _period_range(p: str, f: str | None, t: str | None):
    from datetime import date
    today = datetime.now(KST).date()
    if p == "today":
        return today, today
    if p == "week":
        return today - timedelta(days=today.weekday()), today
    if p == "month":
        return today.replace(day=1), today
    if p == "3m":
        return today - timedelta(days=90), today
    if p == "year":
        return today.replace(month=1, day=1), today
    if p == "custom":
        try:
            return (date.fromisoformat(f) if f else None), (date.fromisoformat(t) if t else None)
        except ValueError:
            return None, None
    return None, None


def bars_svg(items: list[tuple[str, float]]) -> str:
    """기간 손익 막대 (외부 라이브러리 없이). items: [(라벨, 금액)]"""
    if not items:
        return ""
    w, h, pad = 600, 170, 18
    hi = max(max(v for _, v in items), 0)
    lo = min(min(v for _, v in items), 0)
    span = (hi - lo) or 1
    zero = pad + (hi / span) * (h - 2 * pad)
    bw = (w - 2 * pad) / len(items)
    out = [f'<svg viewBox="0 0 {w} {h}" class="chart" role="img" aria-label="기간별 실현 손익">',
           f'<line x1="0" x2="{w}" y1="{zero:.1f}" y2="{zero:.1f}" class="base"/>']
    for i, (lab, v) in enumerate(items):
        bh = abs(v) / span * (h - 2 * pad)
        y = zero - bh if v >= 0 else zero
        x = pad + i * bw + bw * 0.15
        out.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw * 0.7:.1f}" height="{max(bh, 1):.1f}" '
                   f'class="{"bar-up" if v >= 0 else "bar-down"}"><title>{lab}: {v:,.2f}</title></rect>')
    step = max(1, len(items) // 6)
    for i in range(0, len(items), step):
        out.append(f'<text x="{pad + i * bw + bw / 2:.1f}" y="{h - 3}" class="lbl">{items[i][0][5:]}</text>')
    out.append("</svg>")
    return "".join(out)


def _compare_bench(c, j, start, end) -> dict | None:
    """같은 기간 봇 자산 변화(예산 대비) vs 지수 ETF 를 같은 금액으로 들고 있었을 때."""
    series = j.equity_series(c.ledger, 5000)
    if not series:
        return None
    s_iso, e_iso = (start.isoformat() if start else series[0]["ts"][:10]), (end.isoformat() if end else "9999")
    before = [x for x in series if x["ts"][:10] < s_iso]
    inside = [x for x in series if s_iso <= x["ts"][:10] <= e_iso]
    if not inside:
        return None
    e0 = before[-1]["equity"] if before else inside[0]["equity"]
    daily = {}
    for x in inside:
        daily[x["ts"][:10].replace("-", "")] = x["equity"]
    budget = float(c.total_krw) or 1
    out = {"bot_pct": round((inside[-1]["equity"] - e0) / budget * 100, 2), "bench_name": BENCH[c.market][1],
           "bench_pct": None, "chart": ""}
    try:
        from trader import backtest
        from trader.runner import make_broker
        days = 30 + (datetime.now(KST).date() - datetime.fromisoformat(s_iso).date()).days
        bars = backtest.load_bars(make_broker(c), BENCH[c.market][0], max(40, int(days * 0.75) + 10),
                                  c.data_dir / "bars" / c.market)
        s_key, e_key = s_iso.replace("-", ""), e_iso.replace("-", "")
        b0 = next((b["close"] for b in reversed(bars) if b["date"] < s_key), None) or \
            next((b["close"] for b in bars if b["date"] >= s_key), None)
        b1 = next((b["close"] for b in reversed(bars) if b["date"] <= e_key), None)
        if b0 and b1:
            out["bench_pct"] = round((b1 / b0 - 1) * 100, 2)
            bmap = {b["date"]: b["close"] for b in bars}
            ds = sorted(daily)
            if len(ds) >= 2:
                out["chart"] = lines_svg(
                    [[budget + daily[d] - e0 for d in ds],
                     [budget * bmap[d] / b0 if d in bmap else None for d in ds]],
                    ["봇", "지수"], ["up" if daily[ds[-1]] >= e0 else "down", "bench"])
    except Exception as e:  # noqa: BLE001
        out["bench_err"] = str(e)[:120]
    return out


def combine(parts: list[tuple[dict, str]]) -> dict:
    """시장별 기간 손익(ledger.period)을 원화로 합친다. parts: [(r, 통화)]
    미국 거래는 거래마다 당시 환율로 계산한 원화 손익(pnl_krw — 환차손익 포함)을 쓴다."""
    trades, by_code, by_day = [], {}, {}
    buy_amt = 0.0
    buys = 0
    for r, cur in parts:
        for tr in r["trades"]:
            t = {**tr, "pnl": tr["pnl_krw"], "cost": tr["cost_krw"], "cur": cur,
                 "pnl_native": tr["pnl"]}
            trades.append(t)
            g = by_code.setdefault(t["code"], {"code": t["code"], "name": t["name"], "pnl": 0.0, "n": 0})
            g["pnl"] += t["pnl"]
            g["n"] += 1
            by_day[t["date"]] = by_day.get(t["date"], 0.0) + t["pnl"]
        buy_amt += r.get("buy_amount_krw", r["buy_amount"]) or 0
        buys += r["buys"]
    trades.sort(key=lambda t: t["ts"], reverse=True)
    n = len(trades)
    wins = sum(1 for t in trades if t["pnl"] > 0)
    return {"trades": trades, "pnl": round(sum(t["pnl"] for t in trades)), "n": n,
            "costs": round(sum(t["cost"] for t in trades)), "wins": wins,
            "win_rate": round(wins / n * 100, 1) if n else None,
            "best": max(trades, key=lambda t: t["pnl"]) if trades else None,
            "worst": min(trades, key=lambda t: t["pnl"]) if trades else None,
            "by_code": sorted(by_code.values(), key=lambda b: -b["pnl"]),
            "by_day": sorted(by_day.items()), "buys": buys, "buy_amount": round(buy_amt)}


def _pnl_all(base, p, f, t, start, end):
    """국내 + 미국 통합 (원화). 미국 실현 손익은 거래 당시 환율, 보유 평가·AI 비용은 최근 환율로 환산."""
    from trader import ledger, pricing
    kr, us = base.for_market("kr"), base.for_market("us")
    j = Journal(kr.db_path)
    fx = float(j.get("fx_usdkrw") or 0) or 1400.0
    r_kr = ledger.period(j, kr.ledger, start, end, ledger.costs_of(kr))
    j_us = Journal(us.db_path)
    r_us = ledger.period(j_us, us.ledger, start, end, ledger.costs_of(us))
    r = combine([(r_kr, "KRW"), (r_us, "USD")])
    days = r["by_day"]
    monthly = len(days) > 45
    if monthly:
        agg: dict[str, float] = {}
        for d, v in days:
            agg[d[:7] + "-01"] = agg.get(d[:7] + "-01", 0) + v
        days = sorted(agg.items())
    ai = pricing.ai_cost(j, kr.data_dir, None, start, end)
    ai["local"] = round(ai["usd"] * fx)
    ai["fx"] = fx
    ai["net"] = round(r["pnl"] - ai["local"])
    snap = None
    sk, su = j.snapshot(kr.ledger) or {}, j_us.snapshot(us.ledger) or {}
    if sk.get("budget") is not None or su.get("budget") is not None:
        snap = {k: float(sk.get(k) or 0) + float(su.get(k) or 0) * fx for k in ("budget", "unrealized", "pnl")}
        snap["pnl_pct"] = snap["pnl"] / snap["budget"] * 100 if snap["budget"] else 0.0
    split = {"kr": r_kr["pnl"], "us": r_us["pnl"], "us_krw": r_us["pnl_krw"],
             "fx_gain": r_us["pnl_krw"] - round(r_us["pnl"] * fx)}
    return render_template("pnl.html", c=kr, base=base, market="all", r=r, periods=PERIODS, p=p, cmp=None, ai=ai,
                           start=start, end=end, f=f or "", t=t or "", chart=bars_svg(days), monthly=monthly,
                           snap=snap, split=split, fx=fx)


@app.get("/pnl")
@login_required
def pnl():
    from trader import ledger
    base = webapp.cfg()
    p = request.args.get("p", "month")
    f, t = request.args.get("from"), request.args.get("to")
    start, end = _period_range(p, f, t)
    if request.args.get("m") == "all":
        session["pnl_all"] = True
    elif request.args.get("m") in ("kr", "us"):
        session["pnl_all"] = False
    if session.get("pnl_all") and base.us_enabled:
        return _pnl_all(base, p, f, t, start, end)
    m = webapp.current_market()
    c = base.for_market(m)
    j = Journal(c.db_path)
    r = ledger.period(j, c.ledger, start, end, ledger.costs_of(c))
    days = r["by_day"]
    monthly = len(days) > 45
    if monthly:
        agg: dict[str, float] = {}
        for d, v in days:
            agg[d[:7] + "-01"] = agg.get(d[:7] + "-01", 0) + v
        days = sorted(agg.items())
    cmp_ = _compare_bench(c, j, start, end)
    from trader import pricing
    ai = pricing.ai_cost(j, c.data_dir, c.ledger, start, end)
    fx = float(j.get("fx_usdkrw") or 0) or 1400.0
    ai["local"] = ai["usd"] if c.currency == "USD" else round(ai["usd"] * fx)
    ai["fx"] = fx
    ai["net"] = round(r["pnl"] - ai["local"], 2)
    if cmp_ and snap_budget(j, c):
        ai["net_pct"] = round(cmp_["bot_pct"] - ai["local"] / snap_budget(j, c) * 100, 2)
    return render_template("pnl.html", c=c, base=base, market=m, r=r, periods=PERIODS, p=p, cmp=cmp_, ai=ai,
                           start=start, end=end, f=f or "", t=t or "", chart=bars_svg(days), monthly=monthly,
                           snap=j.snapshot(c.ledger))


def snap_budget(j, c) -> float:
    s = j.snapshot(c.ledger) or {}
    return float(s.get("budget") or c.total_krw or 0)


def _usage_summary(j, st: dict, since: str | None) -> dict:
    from trader import pricing
    groups: dict[tuple, dict] = {}
    for r in j.usage(since):
        k = (r["provider"], r["model"])
        g = groups.setdefault(k, {"provider": r["provider"], "model": r["model"], "calls": 0, "tin": 0, "tout": 0,
                                  "cost": 0.0, "known": True})
        g["calls"] += 1
        g["tin"] += r["tin"]
        g["tout"] += r["tout"]
        c_ = pricing.cost(r["provider"], r["model"], r["tin"], r["tout"], st)
        if c_ is None:
            g["known"] = False
        else:
            g["cost"] += c_
    rows = sorted(groups.values(), key=lambda g: -g["cost"])
    return {"rows": rows, "cost": sum(g["cost"] for g in rows), "calls": sum(g["calls"] for g in rows),
            "tin": sum(g["tin"] for g in rows), "tout": sum(g["tout"] for g in rows)}


@app.get("/usage")
@login_required
def usage():
    from trader import pricing
    from trader.analyst import PROVIDERS, available
    c = webapp.cfg()
    j = Journal(c.db_path)
    st = pricing.settings(c.data_dir)
    today = datetime.now(KST).date()
    p = request.args.get("p", "month")
    since = {"month": today.replace(day=1).isoformat(), "30d": (today - timedelta(days=29)).isoformat(),
             "all": None}.get(p, today.replace(day=1).isoformat())
    summ = _usage_summary(j, st, since)
    # 최근 30일로 하루 평균 → 한 달 예상, $충전 시 사용 기간, 충전 잔액
    recent = j.usage((today - timedelta(days=29)).isoformat())
    first = min((r["ts"][:10] for r in recent), default=today.isoformat())
    days = max(1, min(30, (today - datetime.fromisoformat(first).date()).days + 1))
    proj = {}
    for prov in PROVIDERS:
        rs = [r for r in recent if r["provider"] == prov]
        costs = [pricing.cost(prov, r["model"], r["tin"], r["tout"], st) for r in rs]
        known = [x for x in costs if x is not None]
        daily = sum(known) / days
        cr = st["credits"].get(prov) or {}
        used = remaining = left_days = None
        if cr.get("amount"):
            used = sum(x for r in j.usage(cr.get("since") or None) if r["provider"] == prov
                       for x in [pricing.cost(prov, r["model"], r["tin"], r["tout"], st)] if x is not None)
            remaining = float(cr["amount"]) - used
            left_days = (remaining / daily) if daily > 0 else None
        proj[prov] = {"label": pricing.PROVIDER_LABEL[prov], "calls": len(rs), "daily": daily, "monthly": daily * 30,
                      "unknown": len(costs) - len(known), "free": bool(st["free"].get(prov)),
                      "months": (float(st["budget"]) / (daily * 30)) if daily > 0 else None,
                      "has_key": available(prov), "credit": cr, "used": used, "remaining": remaining,
                      "left_days": left_days,
                      "empty_date": (today + timedelta(days=int(left_days))).isoformat() if left_days is not None
                      and left_days < 3650 else None}
    models = sorted({r["model"] for r in j.usage(None)} | set(c.ai_models.values()) | set(pricing.PRICES)
                    | set(st["prices"]), key=lambda m: (pricing.provider_of(m) or "z", m))
    # 모델 바꾸기: 최근 하루 평균 토큰(AI별, 기록 없으면 다른 AI 평균)으로 모델마다 한 달 예상 비용
    tok = {}
    for prov in PROVIDERS:
        rs = [r for r in recent if r["provider"] == prov]
        tok[prov] = (sum(r["tin"] for r in rs) / days, sum(r["tout"] for r in rs) / days) if rs else None
    known = [t for t in tok.values() if t]
    fallback = (sum(t[0] for t in known) / len(known), sum(t[1] for t in known) / len(known)) if known else (6000, 900)
    choices = []
    for key, label, opts in pricing.model_options(settings_io.MODEL_FIELDS, st):
        prov = key.split("_")[0]
        ti, to = tok.get(prov) or fallback
        cur = c.ai_models.get(prov)
        items = []
        for val, desc in opts + ([(cur, f"{cur} (현재 설정)")] if cur not in [o[0] for o in opts] else []):
            pr = pricing.price(val, st)
            items.append({"val": val, "desc": desc, "price": pr,
                          "monthly": None if pr is None else (ti * pr[0] + to * pr[1]) / 1e6 * 30})
        choices.append({"key": key, "label": label, "prov": prov, "cur": cur, "opts": items,
                        "free": bool(st["free"].get(prov)), "basis": "실사용" if tok.get(prov) else "추정"})
    return render_template("usage.html", ms=pricing.month_status(j, c.data_dir), c=c, p=p, summ=summ, proj=proj, st=st, days=days,
                           choices=choices, providers=settings_io.PROVIDER_CHOICES,
                           keys={pv: available(pv) for pv in PROVIDERS},
                           models=[(m, pricing.price(m, st), m in st["prices"], m in pricing.PRICES,
                                    m in c.ai_models.values()) for m in models], today=today)


@app.post("/usage/models")
@login_required
def usage_models():
    import re as _re
    prov = request.form.get("provider", "")
    if prov not in settings_io.PROVIDER_CHOICES:
        flash("AI 선택이 올바르지 않습니다.", "err")
        return redirect(url_for("usage") + "#models")
    text = webapp.CONFIG_PATH.read_text(encoding="utf-8")
    text = settings_io._set(text, "ai", "provider", prov)
    for key, label, _opts in settings_io.MODEL_FIELDS:
        m = (request.form.get(key) or "").strip()
        if not _re.fullmatch(r"[a-z0-9][a-z0-9.\-]{2,60}", m):
            flash(f"{label} 이름이 올바르지 않습니다.", "err")
            return redirect(url_for("usage") + "#models")
        text = settings_io._set(text, "ai", key, m)
    webapp.CONFIG_PATH.write_text(text, encoding="utf-8")
    c = webapp.cfg()
    flash(f"저장했습니다: {settings_io.PROVIDER_CHOICES[prov].split(' (')[0].split(' —')[0]} · "
          + ", ".join(c.ai_models.values()) + " — 다음 실행부터 적용", "ok")
    return redirect(url_for("usage") + "#models")


@app.post("/usage/settings")
@login_required
def usage_settings():
    from trader import pricing
    c = webapp.cfg()
    st = pricing.settings(c.data_dir)
    f = request.form

    def num(k, lo=0.0, hi=1000.0):
        try:
            v = float(str(f.get(k, "")).replace(",", "").replace("$", ""))
        except ValueError:
            return None
        return v if lo <= v <= hi else None
    b = num("budget", 0.5, 1000)
    if b:
        st["budget"] = b
    lim = num("monthly_limit", 0, 1000)
    if lim is not None:
        st["monthly_limit"] = lim
    st["free"] = {"gemini": bool(f.get("free_gemini"))}
    for prov in ("claude", "gemini", "openai"):
        amt, since = num(f"credit_{prov}", 0, 10000), (f.get(f"since_{prov}") or "").strip()
        if amt:
            st["credits"][prov] = {"amount": amt, "since": since or datetime.now(KST).date().isoformat()}
        else:
            st["credits"].pop(prov, None)
    import re as _re
    for k in f:
        if k.startswith("pin_"):
            m = k[4:]
            if f.get("del_" + m):
                st["prices"].pop(m, None)
                continue
            pi, po = num(k, 0, 500), num("pout_" + m, 0, 2000)
            default = pricing.PRICES.get(m)
            if pi is not None and po is not None and (pi, po) != default:
                st["prices"][m] = [pi, po]
            elif default is not None or pi is None:
                st["prices"].pop(m, None)
    nm = (f.get("new_model") or "").strip().lower()
    if nm:
        ni, no = num("new_in", 0, 500), num("new_out", 0, 2000)
        if not _re.fullmatch(r"[a-z0-9][a-z0-9.\-]{2,60}", nm):
            flash("모델 ID 형식이 올바르지 않습니다 (예: claude-haiku-6)", "err")
        elif ni is None or no is None:
            flash("새 모델의 입력·출력 단가를 숫자로 넣으세요.", "err")
        else:
            st["prices"][nm] = [ni, no]
            prov = pricing.provider_of(nm)
            flash(f"모델 추가: {nm} (입력 ${ni:g} / 출력 ${no:g})"
                  + ("" if prov else " — 이름이 claude-/gemini-/gpt- 로 시작하지 않아 모델 선택 목록엔 안 나와요"), "ok")
    pricing.save(c.data_dir, st)
    flash("저장했습니다.", "ok")
    return redirect(url_for("usage") + "#calc")
