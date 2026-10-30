"""AI 자동매매 웹 관리 화면.

  .venv/bin/python -m web.app          # http://<LXC IP>:8080
  .env 의 WEB_PASSWORD 로 로그인합니다.

앱키·계좌·AI 키도 웹(설정 → 연결 정보)에서 넣을 수 있다. 값은 화면에 다시 보여 주지 않고,
저장할 때 웹 비밀번호를 다시 묻고, .env 를 백업(.env.bak)한 뒤 웹을 자동 재시작한다.
(.env 에 WEB_ENV_EDIT=off 면 웹에서 못 바꿈. 안전장치 키들은 웹에서 절대 못 바꿈)
실계좌 전환은 웹에서 가능하지만 비밀번호 재입력 + 확인 문구 + 연결 확인을 거친다
(.env 에 WEB_LIVE_SWITCH=off 면 웹에서 실계좌로 못 바꿈).
"""
from __future__ import annotations

import fcntl
import hmac
import os
import secrets
import sys
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from functools import wraps
from pathlib import Path

from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trader import config as cfgmod  # noqa: E402
from trader.journal import KST, Journal  # noqa: E402
from trader import universe  # noqa: E402
from trader.runner import Busy, refresh_balance, run_cycle, run_scan  # noqa: E402
from web import settings_io  # noqa: E402

app = Flask(__name__)


@app.context_processor
def _inject_version():
    from trader import __version__
    return {"app_version": __version__}
CONFIG_PATH = cfgmod.ensure_config()
def _sched(market: str) -> list[tuple[int, int]]:
    """자동 실행 시각 (config.toml [schedule], 없으면 기본값) — ‘다음 실행’ 표시용."""
    from trader import schedule
    try:
        raw = settings_io.read(CONFIG_PATH)
    except Exception:  # noqa: BLE001
        raw = {}
    return schedule.as_tuples(schedule.times(raw, market))


def cfg():
    return cfgmod.load()


def _secret_key() -> str:
    c = cfg()
    f = c.data_dir / "web_secret"
    if not f.exists():
        f.write_text(secrets.token_hex(32))
        os.chmod(f, 0o600)
    return f.read_text().strip()


app.secret_key = _secret_key()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Strict",
                  PERMANENT_SESSION_LIFETIME=timedelta(days=7))

# ── 백그라운드 실행 상태 ───────────────────────────────
_job = {"running": False, "kind": None, "started": None}
_job_lock = threading.Lock()


def _busy(c) -> bool:
    """웹 작업 또는 타이머/명령줄 실행이 잠금을 잡고 있는지."""
    if _job["running"]:
        return True
    try:
        with open(c.data_dir / "run.lock", "w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fh, fcntl.LOCK_UN)
        return False
    except BlockingIOError:
        return True


def _start_job(kind: str, market: str = "kr", params: dict | None = None) -> str | None:
    c = cfg()
    with _job_lock:
        if _busy(c):
            return "이미 실행 중입니다."
        _job.update(running=True, kind=kind, started=time.time())

    def work():
        try:
            if kind == "scan":
                run_scan(cfg(), source="web")
            elif kind == "backtest":
                _run_backtest(market, params or {})
            else:
                run_cycle(cfg(), analyze_only=(kind == "analyze"), force=False, source="web", market=market)
        except Busy:
            pass
        finally:
            _job.update(running=False)

    threading.Thread(target=work, daemon=True).start()
    return None


# ── 앱(PWA) — 홈 화면에 추가했을 때 이름·아이콘·전체 화면 ─────
@app.get("/manifest.webmanifest")
def manifest():
    st = lambda f: url_for("static", filename=f)  # noqa: E731
    data = {
        "name": "AI 자동매매", "short_name": "AI 자동매매", "lang": "ko", "start_url": "/", "scope": "/",
        "display": "standalone", "background_color": "#0f172a", "theme_color": "#141a24",
        "icons": [{"src": st("icon-192.png"), "sizes": "192x192", "type": "image/png", "purpose": "any"},
                  {"src": st("icon-512.png"), "sizes": "512x512", "type": "image/png", "purpose": "any"},
                  {"src": st("icon-512.png"), "sizes": "512x512", "type": "image/png", "purpose": "maskable"}],
    }
    import json as _json
    return app.response_class(_json.dumps(data, ensure_ascii=False), mimetype="application/manifest+json")


@app.get("/apple-touch-icon.png")
@app.get("/apple-touch-icon-precomposed.png")
@app.get("/favicon.ico")
def root_icons():
    """iOS·브라우저가 사이트 루트에서 직접 찾는 아이콘."""
    from flask import send_from_directory
    f = "favicon.png" if request.path.endswith(".ico") else "apple-touch-icon.png"
    return send_from_directory(app.static_folder, f, mimetype="image/png", max_age=86400)


# ── 실행 누락 감시 (웹이 켜져 있는 동안 5분마다) ──────────────
def _monitor_loop():
    from trader import monitor, notify
    while True:
        time.sleep(300)
        try:
            c = cfg()
            raw = settings_io.read(CONFIG_PATH)
            j = Journal(c.db_path)
            monitor.check(j, c, raw, notify.send)
            from trader import backup
            if datetime.now(KST).hour >= 18:           # 장 끝난 뒤 오늘 백업이 아직 없으면
                backup.daily(j, c.data_dir, raw, warn=lambda m: notify.send(c, "errors", m))
        except Exception as e:  # noqa: BLE001
            app.logger.warning("누락 감시·백업 오류: %s", e)


# ── 인증 ───────────────────────────────────────────────
def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not session.get("ok"):
            return redirect(url_for("login", next=request.path))
        return fn(*a, **kw)
    return wrapper


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        pw = os.environ.get("WEB_PASSWORD", "")
        if pw and hmac.compare_digest(request.form.get("password", ""), pw):
            session.clear()
            session["ok"] = True
            session.permanent = True
            nxt = request.args.get("next", "/")
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else "/")
        time.sleep(1.5)  # 무차별 대입 지연
        flash("비밀번호가 틀렸습니다.", "err")
    from web import passkeys
    return render_template("login.html", has_passkeys=bool(passkeys.load(cfg().data_dir)))


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ── 화면 ───────────────────────────────────────────────
ET = ZoneInfo("America/New_York")


def next_run_us(now: datetime) -> datetime:
    n = now.astimezone(ET)
    for d in range(8):
        day = n.date() + timedelta(days=d)
        if day.weekday() >= 5:
            continue
        for h, m in _sched("us"):
            t = datetime(day.year, day.month, day.day, h, m, tzinfo=ET)
            if t > n:
                return t.astimezone(KST)
    return now


def fmt_kst(t: datetime) -> str:
    return t.strftime('%m/%d') + '(' + '월화수목금토일'[t.weekday()] + ') ' + t.strftime('%H:%M')


def current_market() -> str:
    """?m=kr|us 로 바꾸고 세션에 기억. 미국이 꺼져 있으면 항상 국내."""
    m = request.args.get("m") or request.form.get("market") or session.get("market") or "kr"
    if m not in ("kr", "us") or (m == "us" and not cfg().us_enabled):
        m = "kr"
    session["market"] = m
    return m


def next_run(now: datetime) -> datetime:
    for d in range(8):
        day = now.date() + timedelta(days=d)
        if day.weekday() >= 5:
            continue
        for h, m in _sched("kr"):
            t = datetime(day.year, day.month, day.day, h, m, tzinfo=KST)
            if t > now:
                return t
    return now


def equity_svg(series: list[dict], base: int) -> str:
    """자산 추이 SVG (외부 라이브러리 없이)."""
    pts = [s["equity"] for s in series if s.get("equity")]
    if len(pts) < 2:
        return ""
    w, h, pad = 600, 160, 6
    lo, hi = min(pts + [base]), max(pts + [base])
    span = (hi - lo) or 1

    def xy(i, v):
        return pad + i * (w - 2 * pad) / (len(pts) - 1), h - pad - (v - lo) * (h - 2 * pad) / span

    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in (xy(i, v) for i, v in enumerate(pts)))
    by = xy(0, base)[1]
    up = pts[-1] >= base
    return (f'<svg viewBox="0 0 {w} {h}" class="chart" role="img" aria-label="자산 추이">'
            f'<line x1="0" x2="{w}" y1="{by:.1f}" y2="{by:.1f}" class="base"/>'
            f'<polyline points="{line}" class="line {"up" if up else "down"}"/></svg>')


@app.get("/")
@login_required
def index():
    m = current_market()
    base = cfg()
    c = base.for_market(m)
    j = Journal(c.db_path)
    snap = j.snapshot(c.ledger)
    stopped = c.stop_file.exists()
    halted = j.get(f"halted_{c.ledger}") == "1"
    now = datetime.now(KST)
    from trader import monitor
    from trader.runner import broker_name
    try:
        missed = monitor.missed_slots(j, base, settings_io.read(CONFIG_PATH), m) if monitor.timer_installed(m) else []
    except Exception:  # noqa: BLE001
        missed = []
    return render_template(
        "index.html", c=c, base=base, market=m, broker=broker_name(c), missed=missed,
        watch_last=(j.get(f"watch_last_{c.ledger}") or "")[11:16],
        snap=snap, stopped=stopped, halted=halted, busy=_busy(c), job=_job,
        next_run=fmt_kst(next_run(now) if m == "kr" else next_run_us(now)),
        chart=equity_svg(j.equity_series(c.ledger), c.total_krw),
        runs=j.runs_with_decisions(c.ledger, 5), orders=j.orders(c.ledger, 10),
        logs=j.logs(5), orders_today=j.orders_today(c.ledger),
        auto=universe.saved(j) if m == "kr" else None, today=now.strftime("%Y-%m-%d"),
    )


@app.get("/history")
@login_required
def history():
    m = current_market()
    base = cfg()
    c = base.for_market(m)
    j = Journal(c.db_path)
    return render_template("history.html", c=c, base=base, market=m, runs=j.runs_with_decisions(c.ledger, 30),
                           orders=j.orders(c.ledger, 200), logs=j.logs(50))


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


@app.get("/pnl")
@login_required
def pnl():
    from trader import ledger
    m = current_market()
    base = cfg()
    c = base.for_market(m)
    j = Journal(c.db_path)
    p = request.args.get("p", "month")
    f, t = request.args.get("from"), request.args.get("to")
    start, end = _period_range(p, f, t)
    r = ledger.period(j, c.ledger, start, end, ledger.costs_of(c))
    days = r["by_day"]
    monthly = len(days) > 45
    if monthly:
        agg: dict[str, float] = {}
        for d, v in days:
            agg[d[:7] + "-01"] = agg.get(d[:7] + "-01", 0) + v
        days = sorted(agg.items())
    cmp_ = _compare_bench(c, j, start, end)
    return render_template("pnl.html", c=c, base=base, market=m, r=r, periods=PERIODS, p=p, cmp=cmp_,
                           start=start, end=end, f=f or "", t=t or "", chart=bars_svg(days), monthly=monthly,
                           snap=j.snapshot(c.ledger))


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
    c = cfg()
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
    return render_template("usage.html", c=c, p=p, summ=summ, proj=proj, st=st, days=days,
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
    text = CONFIG_PATH.read_text(encoding="utf-8")
    text = settings_io._set(text, "ai", "provider", prov)
    for key, label, _opts in settings_io.MODEL_FIELDS:
        m = (request.form.get(key) or "").strip()
        if not _re.fullmatch(r"[a-z0-9][a-z0-9.\-]{2,60}", m):
            flash(f"{label} 이름이 올바르지 않습니다.", "err")
            return redirect(url_for("usage") + "#models")
        text = settings_io._set(text, "ai", key, m)
    CONFIG_PATH.write_text(text, encoding="utf-8")
    c = cfg()
    flash(f"저장했습니다: {settings_io.PROVIDER_CHOICES[prov].split(' (')[0].split(' —')[0]} · "
          + ", ".join(c.ai_models.values()) + " — 다음 실행부터 적용", "ok")
    return redirect(url_for("usage") + "#models")


@app.post("/usage/settings")
@login_required
def usage_settings():
    from trader import pricing
    c = cfg()
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


@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    if request.method == "POST":
        errors = settings_io.validate_and_write(CONFIG_PATH, request.form)
        if errors:
            for e in errors:
                flash(e, "err")
        else:
            flash("저장했습니다. 다음 실행부터 적용됩니다.", "ok")
            return redirect(url_for("settings"))
    raw = settings_io.read(CONFIG_PATH)
    from trader.ledger import DEFAULT_COSTS
    fields = [(key, label, raw.get(sec, {}).get(key, DEFAULT_COSTS.get(key, "")))
              for sec, key, _t, _lo, _hi, label in settings_io.FIELDS]
    auto_fields = [(key, label, raw.get(sec, {}).get(key, settings_io.DEFAULTS.get(key, "")))
                   for sec, key, _t, _lo, _hi, label in settings_io.AUTO_FIELDS]
    us_fields = [(key, label, raw.get("us", {}).get(key, settings_io.US_DEFAULTS.get(key, "")))
                 for _sec, key, _t, _lo, _hi, label in settings_io.US_FIELDS]
    us_raw = {"enabled": False, "margin_currency": "krw", "symbols": [], **raw.get("us", {})}
    from trader.runner import broker_name
    us_broker = broker_name(cfg().for_market("us"))
    from trader.analyst import PROVIDERS, available
    return render_template("settings.html", c=cfg(), fields=fields,
                           env_groups=_env_view(), web_env=_web_env_edit(),
                           upd=__import__("trader.updater", fromlist=["x"]).last_check(cfg().data_dir),
                           web_update=_web_update(),
                           sched={mk: ", ".join(__import__("trader.schedule", fromlist=["x"]).times(raw, mk))
                                  for mk in ("kr", "us")},
                           watch_min=__import__("trader.schedule", fromlist=["x"]).watch_minutes(raw),
                           notify_kinds=__import__("trader.notify", fromlist=["x"]).KINDS,
                           notify_on={k: __import__("trader.notify", fromlist=["x"]).enabled(cfg(), k)
                                      for k in __import__("trader.notify", fromlist=["x"]).KINDS},
                           tg_ready=__import__("trader.notify", fromlist=["x"]).telegram_ready(),
                           tg_token=bool(os.environ.get("TELEGRAM_BOT_TOKEN")),
                           kakao_key=bool(os.environ.get("KAKAO_REST_KEY")),
                           kakao=__import__("trader.notify", fromlist=["x"]).kakao_info(cfg().data_dir),
                           kakao_redirect=_public_base() + url_for("kakao_callback"),
                           backups=__import__("trader.backup", fromlist=["x"]).listing(cfg().data_dir),
                           bk=__import__("trader.backup", fromlist=["x"]).settings(raw),
                           rmt=__import__("trader.remote", fromlist=["x"]).settings(raw),
                           rmt_types=__import__("trader.remote", fromlist=["x"]).TYPES,
                           passkey_list=__import__("web.passkeys", fromlist=["x"]).load(cfg().data_dir),
                           acct_choice=session.get("acct_choice"),
                           mode_label=MODE_LABEL, live_phrase=LIVE_PHRASE, web_live=_web_live_switch(),
                           env_mode=os.environ.get("TRADING_MODE", "dry").strip().lower(), raw=raw, auto_fields=auto_fields,
                           us_fields=us_fields, us=us_raw, us_broker=us_broker,
                           order_types=settings_io.ORDER_TYPES, providers=settings_io.PROVIDER_CHOICES,
                           model_fields=__import__("trader.pricing", fromlist=["x"]).model_options(
                               settings_io.MODEL_FIELDS, __import__("trader.pricing", fromlist=["x"]).settings(cfg().data_dir)),
                           keys={p: available(p) for p in PROVIDERS},
                           key_labels={p: i["label"] for p, i in PROVIDERS.items()})


# ── 실행 모드 전환 ─────────────────────────────────────
MODE_LABEL = {"dry": "드라이런 (주문 안 냄)", "mock": "모의투자", "live": "실계좌"}
LIVE_PHRASE = "실계좌 전환"


def _web_live_switch() -> bool:
    return os.environ.get("WEB_LIVE_SWITCH", "on").strip().lower() not in ("off", "no", "0", "false")


def _mode_markets(c) -> list[str]:
    return ["kr"] + (["us"] if c.us_enabled else [])


def _check_mode(c, new: str) -> tuple[list[str], list[str], dict | None]:
    """새 모드로 증권사에 실제로 붙어 보고 (오류, 결과 요약, 계좌 선택 필요 정보) 반환. 실패하면 전환하지 않는다."""
    from dataclasses import replace
    from trader.broker import AccountChoice
    from trader.runner import broker_name, make_broker
    errs, info, choice = [], [], None
    for mk in _mode_markets(c):
        mc = replace(c, mode=new).for_market(mk)
        name = broker_name(mc)
        flag = "🇰🇷" if mk == "kr" else "🇺🇸"
        if new == "mock" and name == "toss":
            errs.append(f"{flag} 토스증권은 모의투자가 없습니다 (드라이런 또는 실계좌만 가능)")
            continue
        if new == "dry":
            continue
        try:
            b = make_broker(mc)
            bal = b.balance()
            cash = "확인 필요" if bal.orderable_cash == float("inf") else mc.money(bal.orderable_cash)
            info.append(f"{flag} {name} 계좌 {getattr(b, 'account', '-')} · 주문가능 {cash}")
        except AccountChoice as e:
            if choice is None:
                choice = {"mode": new, "env": e.env, "accounts": _describe_accounts(e.candidates, new)}
            errs.append(f"{flag} {name} 계좌가 여러 개라 하나를 골라야 합니다 — 아래 ‘실행 모드’에서 고르세요")
        except Exception as e:  # noqa: BLE001
            errs.append(f"{flag} {name} {MODE_LABEL[new]} 연결 실패: {e}")
    return errs, info, choice


def _describe_accounts(cands: list[str], mode: str) -> list[list[str]]:
    """계좌 후보마다 주문가능금액·보유 종목 수를 붙여 고르기 쉽게."""
    from trader.broker import NHBroker
    out = []
    for a in cands:
        try:
            bal = NHBroker(mode, account=a).balance()
            out.append([a, f"주문가능 {bal.orderable_cash:,.0f}원 · 보유 {len(bal.holdings)}종목"])
        except Exception:  # noqa: BLE001
            out.append([a, "잔고 조회 안 됨"])
    return out


@app.post("/mode")
@login_required
def change_mode():
    from trader import modefile
    c = cfg()
    new = request.form.get("mode", "")
    if new == "env":                                   # 웹 설정을 지우고 .env 값으로
        modefile.clear(c.data_dir)
        flash(f"웹 모드 설정을 지웠습니다. 이제 .env 의 TRADING_MODE 를 따릅니다 (현재 {cfg().mode}).", "ok")
        return redirect(url_for("settings"))
    if new not in modefile.MODES:
        flash("알 수 없는 모드입니다.", "err")
        return redirect(url_for("settings"))
    if _busy(c):
        flash("실행 중에는 모드를 바꿀 수 없습니다. 끝난 뒤 다시 시도하세요.", "err")
        return redirect(url_for("settings"))
    if new == "live":
        if not _web_live_switch():
            flash("웹에서 실계좌 전환이 꺼져 있습니다 (.env 의 WEB_LIVE_SWITCH=off).", "err")
            return redirect(url_for("settings"))
        pw = os.environ.get("WEB_PASSWORD", "")
        if not (pw and hmac.compare_digest(request.form.get("password", ""), pw)):
            flash("비밀번호가 맞지 않습니다.", "err")
            return redirect(url_for("settings"))
        if request.form.get("phrase", "").strip() != LIVE_PHRASE:
            flash(f"확인 문구를 정확히 입력하세요: {LIVE_PHRASE}", "err")
            return redirect(url_for("settings"))
        if not request.form.get("agree"):
            flash("실제 돈으로 주문된다는 데 동의해야 합니다.", "err")
            return redirect(url_for("settings"))
    acct = request.form.get("account", "").strip()
    pending = session.get("acct_choice") or {}
    if acct:                                           # 여러 계좌 중 하나를 고른 경우 → 환경별 계좌로 저장
        if pending.get("mode") != new or acct not in [a for a, _ in pending.get("accounts", [])]:
            flash("계좌 선택이 만료됐습니다. 다시 시도하세요.", "err")
            session.pop("acct_choice", None)
            return redirect(url_for("settings") + "#mode")
        from trader import envfile
        key = f"NHPLUG_ACCOUNT_{pending['env'].upper()}"
        envfile.update({key: acct})
        os.environ[key] = acct
    errs, info, choice = _check_mode(c, new)
    if choice:
        session["acct_choice"] = choice
    else:
        session.pop("acct_choice", None)
    if errs:
        for e in errs:
            flash(e, "err")
        flash(f"{MODE_LABEL[new]}(으)로 바꾸지 않았습니다.", "err")
        return redirect(url_for("settings") + "#mode")
    old = c.mode
    modefile.write(c.data_dir, new, live_ack=(new == "live"))
    msg = f"실행 모드: {MODE_LABEL[old]} → {MODE_LABEL[new]}" + ("".join("\n" + x for x in info))
    Journal(c.db_path).add_log(new, "mode", "web", msg)
    flash(("🔴 " if new == "live" else "") + msg.replace("\n", " · ") + " — 다음 실행부터 적용됩니다.", "ok")
    return redirect(url_for("settings"))


# ── 연결 정보 (.env) ─────────────────────────────────────
# (키, 이름, 종류) 종류: secret=값 숨김 · id=끝 4자리만 · plain=그대로
ENV_GROUPS = [
    ("증권사 선택", "broker", [
        ("BROKER", "국내 주식 증권사", "choice:nh=NH 나무증권,kis=한국투자증권,toss=토스증권"),
        ("BROKER_US", "미국 주식 증권사", "choice:=국내와 같게 (kis면 NH),nh=NH 나무증권,toss=토스증권"),
    ]),
    ("NH 나무증권 (nhplug.com)", "nh", [
        ("NHPLUG_APP_KEY", "앱키 (App Key)", "id"),
        ("NHPLUG_APP_SECRET", "앱 시크릿 (App Secret)", "secret"),
        ("NHPLUG_ACCOUNT", "계좌번호 — 여러 개일 때만", "id"),
        ("NHPLUG_ACCOUNT_MOCK", "모의투자 계좌 — 따로 고정할 때", "id"),
        ("NHPLUG_ACCOUNT_LIVE", "실계좌 계좌 — 따로 고정할 때", "id"),
        ("NHPLUG_ACCOUNT_US", "미국 주식 계좌 — 다를 때만", "id"),
    ]),
    ("AI API 키 — 쓸 것만", "ai", [
        ("GEMINI_API_KEY", "Gemini (aistudio.google.com)", "secret"),
        ("ANTHROPIC_API_KEY", "Claude (console.anthropic.com)", "secret"),
        ("OPENAI_API_KEY", "ChatGPT (platform.openai.com)", "secret"),
        ("DART_API_KEY", "DART 공시 키 — 뉴스·공시 켤 때 (opendart.fss.or.kr, 무료)", "secret"),
    ]),
    ("알림 — 텔레그램 · 카카오톡 (선택)", "notify", [
        ("TELEGRAM_BOT_TOKEN", "텔레그램 봇 토큰 (@BotFather)", "secret"),
        ("TELEGRAM_CHAT_ID", "텔레그램 채팅 ID — 알림 칸의 ‘찾기’로 자동 입력", "plain"),
        ("KAKAO_REST_KEY", "카카오 REST API 키 (developers.kakao.com)", "id"),
        ("KAKAO_CLIENT_SECRET", "카카오 Client Secret — 켠 경우만", "secret"),
        ("HEALTHCHECK_URL", "헬스체크 주소 — 서버 꺼짐 감시 (healthchecks.io 등)", "secret"),
        ("HEALTHCHECK_URL_US", "미국용 헬스체크 주소 — 따로 볼 때만", "secret"),
    ]),
    ("한국투자증권 (선택)", "kis", [
        ("KIS_APP_KEY", "모의투자 앱키", "id"),
        ("KIS_APP_SECRET", "모의투자 앱 시크릿", "secret"),
        ("KIS_ACCOUNT", "모의투자 계좌 (12345678-01)", "id"),
        ("KIS_LIVE_APP_KEY", "실전 앱키", "id"),
        ("KIS_LIVE_APP_SECRET", "실전 앱 시크릿", "secret"),
        ("KIS_LIVE_ACCOUNT", "실전 계좌", "id"),
    ]),
    ("토스증권 (선택)", "toss", [
        ("TOSS_CLIENT_ID", "Client ID", "id"),
        ("TOSS_CLIENT_SECRET", "Client Secret", "secret"),
        ("TOSS_ACCOUNT_SEQ", "accountSeq — 여러 계좌일 때만", "plain"),
    ]),
]
ENV_KEYS = {k: kind for _t, _g, items in ENV_GROUPS for k, _l, kind in items}


def _web_env_edit() -> bool:
    return os.environ.get("WEB_ENV_EDIT", "on").strip().lower() not in ("off", "no", "0", "false")


def _env_view() -> list:
    from trader import envfile
    cur = envfile.read()
    groups = []
    for title, gid, items in ENV_GROUPS:
        rows = []
        for k, label, kind in items:
            v = cur.get(k, "")
            if kind.startswith("choice:"):
                opts = [tuple(o.split("=", 1)) for o in kind[7:].split(",")]
                rows.append({"key": k, "label": label, "kind": "choice", "value": v, "options": opts})
                continue
            shown = ("" if not v else "설정됨 ✅" if kind == "secret"
                     else ("•••• " + v[-4:]) if kind == "id" and len(v) > 4 else v)
            rows.append({"key": k, "label": label, "kind": kind, "set": bool(v), "shown": shown})
        groups.append({"title": title, "id": gid, "rows": rows})
    return groups


def _restart_soon():
    """systemd 가 다시 띄우도록 잠시 뒤 종료 → 바뀐 .env 를 웹·증권사 모듈 모두 새로 읽는다."""
    if app.config.get("TESTING"):
        return
    threading.Timer(1.5, lambda: os._exit(3)).start()


@app.post("/env")
@login_required
def change_env():
    from trader import envfile
    c = cfg()
    if not _web_env_edit():
        flash("웹에서 연결 정보 변경이 꺼져 있습니다 (.env 의 WEB_ENV_EDIT=off).", "err")
        return redirect(url_for("settings"))
    pw = os.environ.get("WEB_PASSWORD", "")
    if not (pw and hmac.compare_digest(request.form.get("current_password", ""), pw)):
        time.sleep(1.0)
        flash("현재 웹 비밀번호가 맞지 않아 저장하지 않았습니다.", "err")
        return redirect(url_for("settings") + "#env")
    if _busy(c):
        flash("실행 중에는 저장할 수 없습니다. 끝난 뒤 다시 시도하세요.", "err")
        return redirect(url_for("settings") + "#env")
    changes, errs = {}, []
    cur = envfile.read()
    for k, kind in ENV_KEYS.items():
        if request.form.get(f"clear_{k}"):
            if k in cur:
                changes[k] = None
            continue
        v = (request.form.get(k) or "").strip()
        if kind.startswith("choice:"):
            allowed = [o.split("=", 1)[0] for o in kind[7:].split(",")]
            if v not in allowed:
                errs.append(f"{k}: 고를 수 없는 값")
            elif v != cur.get(k, ""):
                changes[k] = v or None
            continue
        if not v:                                    # 빈칸 = 그대로 둠
            continue
        e = envfile.check_value(v)
        if e:
            errs.append(f"{k}: {e}")
        elif v != cur.get(k):
            changes[k] = v
    new_pw = request.form.get("new_password", "")
    if new_pw:
        if len(new_pw) < 8:
            errs.append("새 웹 비밀번호는 8자 이상이어야 합니다")
        elif new_pw != request.form.get("new_password2", ""):
            errs.append("새 웹 비밀번호 두 칸이 다릅니다")
        elif envfile.check_value(new_pw) or " #" in new_pw:
            errs.append("새 웹 비밀번호에 따옴표·역슬래시·' #' 은 쓸 수 없습니다")
        else:
            changes["WEB_PASSWORD"] = new_pw
    if errs:
        for e in errs:
            flash(e, "err")
        flash("아무것도 저장하지 않았습니다.", "err")
        return redirect(url_for("settings") + "#env")
    if not changes:
        flash("바뀐 값이 없습니다.", "ok")
        return redirect(url_for("settings") + "#env")
    envfile.update(changes)
    for k, v in changes.items():                 # 지금 프로세스에도 바로 반영
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    names = ", ".join(sorted("웹 비밀번호" if k == "WEB_PASSWORD" else k for k in changes))
    Journal(c.db_path).add_log(c.ledger, "env", "web", f"연결 정보 변경: {names}")
    _restart_soon()
    return render_template("restarting.html", names=names)


# ── 백업 ─────────────────────────────────────────────────
@app.post("/backup/now")
@login_required
def backup_now():
    from trader import backup
    c = cfg()
    try:
        p, notes = backup.make(c.data_dir, settings_io.read(CONFIG_PATH))
        flash(f"백업했습니다: {p.name} ({p.stat().st_size / 1024:,.0f}KB){' · ' + ', '.join(notes) if notes else ''}", "ok")
        Journal(c.db_path).add_log(c.ledger, "backup", "web", f"백업: {p.name}")
    except Exception as e:  # noqa: BLE001
        flash(f"백업 실패: {e}", "err")
    return redirect(url_for("settings") + "#backup")


@app.get("/backup/<name>")
@login_required
def backup_download(name):
    import re as _re
    from flask import abort, send_from_directory
    from trader import backup
    if not _re.fullmatch(r"ai-trader-\d{8}-\d{6}\.zip", name):
        abort(404)
    return send_from_directory(backup.folder(cfg().data_dir), name, as_attachment=True)


RESTORE_PHRASE = "복구"


@app.post("/backup/restore")
@login_required
def backup_restore():
    """서버의 백업 목록에서 고르거나 zip 을 올려 되돌린다 (비밀번호 + 확인 문구, 직전 상태 자동 백업)."""
    import re as _re
    from trader import backup
    c = cfg()
    back = redirect(url_for("settings") + "#backup")
    if not _pw_ok(request.form.get("password", "")):
        time.sleep(1.0)
        flash("웹 비밀번호가 맞지 않습니다.", "err")
        return back
    if request.form.get("phrase", "").strip() != RESTORE_PHRASE:
        flash(f"확인 문구를 정확히 입력하세요: {RESTORE_PHRASE}", "err")
        return back
    if _busy(c):
        flash("매매·분석이 실행 중입니다. 끝난 뒤 다시 시도하세요.", "err")
        return back
    up = request.files.get("file")
    tmp = None
    name = request.form.get("name", "")
    if up and up.filename:
        tmp = c.data_dir / f"upload-restore-{int(time.time())}.zip"
        up.save(tmp)
        src = tmp
    elif name.startswith("remote:"):
        from trader import remote
        tmp = c.data_dir / f"remote-restore-{int(time.time())}.zip"
        try:
            src = remote.download(settings_io.read(CONFIG_PATH), name[7:], tmp)
        except Exception as e:  # noqa: BLE001
            tmp.unlink(missing_ok=True)
            flash(f"원격 백업을 받지 못했습니다: {e}", "err")
            return back
    else:
        if not _re.fullmatch(r"ai-trader-\d{8}-\d{6}\.zip", name) or not (backup.folder(c.data_dir) / name).exists():
            flash("복구할 백업을 고르거나 zip 파일을 올리세요.", "err")
            return back
        src = backup.folder(c.data_dir) / name
    try:
        info = backup.inspect(src)
        done, before = backup.restore(c.data_dir, src, settings_io.read(CONFIG_PATH),
                                      with_env=bool(request.form.get("with_env")))
    except Exception as e:  # noqa: BLE001
        flash(f"복구하지 않았습니다: {e}", "err")
        return back
    finally:
        if tmp:
            tmp.unlink(missing_ok=True)
    Journal(cfg().db_path).add_log(c.ledger, "backup", "web",
                                   f"복구: {', '.join(done)} (주문 {info['orders']}건, 마지막 {info['last_order']}) · "
                                   f"직전 상태는 {before.name} 에 백업")
    _restart_soon()
    return render_template("restarting.html", names=", ".join(done) + f" — 직전 상태는 {before.name} 로 백업해 뒀어요",
                           title="복구했습니다 ✅")


@app.get("/api/remote_backups")
@login_required
def api_remote_backups():
    from trader import remote
    try:
        return jsonify(names=remote.listing(settings_io.read(CONFIG_PATH))[:30])
    except Exception as e:  # noqa: BLE001
        return jsonify(error=str(e)[:200]), 502


@app.post("/backup/test")
@login_required
def backup_test():
    from trader import remote
    try:
        flash("🔌 " + remote.test(settings_io.read(CONFIG_PATH)), "ok")
    except Exception as e:  # noqa: BLE001
        flash(f"원격 저장 연결 실패: {e}", "err")
    return redirect(url_for("settings") + "#backup")


@app.post("/backup/settings")
@login_required
def backup_settings():
    text = CONFIG_PATH.read_text(encoding="utf-8")
    try:
        keep = max(1, min(365, int(request.form.get("keep_days", "14"))))
    except ValueError:
        keep = 14
    extra = (request.form.get("extra_dir") or "").strip()
    if extra and not extra.startswith("/"):
        flash("추가 저장 위치는 / 로 시작하는 서버 경로여야 합니다 (예: /mnt/nas/ai-trader).", "err")
        return redirect(url_for("settings") + "#backup")
    from trader import envfile, remote
    rtype = (request.form.get("remote_type") or "").lower()
    rurl = (request.form.get("remote_url") or "").strip()
    ruser = (request.form.get("remote_user") or "").strip()
    if rtype not in remote.TYPES:
        rtype = ""
    err = remote.check_url(rtype, rurl) if rtype else None
    if err or any(ch in rurl + ruser for ch in '"\n\r'):
        flash(err or "주소·사용자에 따옴표나 줄바꿈은 넣을 수 없습니다.", "err")
        return redirect(url_for("settings") + "#backup")
    text = settings_io._set(text, "backup", "keep_days", keep)
    text = settings_io._set(text, "backup", "extra_dir", extra)
    text = settings_io._set(text, "backup", "include_env", bool(request.form.get("include_env")))
    text = settings_io._set(text, "backup", "remote_type", rtype)
    text = settings_io._set(text, "backup", "remote_url", rurl)
    text = settings_io._set(text, "backup", "remote_user", ruser)
    CONFIG_PATH.write_text(text, encoding="utf-8")
    pw = request.form.get("remote_password") or ""
    if pw or request.form.get("clear_remote_password"):
        if pw and envfile.check_value(pw):
            flash("비밀번호에 따옴표·역슬래시·줄바꿈은 쓸 수 없습니다.", "err")
        else:
            envfile.update({"BACKUP_REMOTE_PASSWORD": pw or None})
            if pw:
                os.environ["BACKUP_REMOTE_PASSWORD"] = pw
            else:
                os.environ.pop("BACKUP_REMOTE_PASSWORD", None)
    flash("백업 설정을 저장했습니다.", "ok")
    return redirect(url_for("settings") + "#backup")


# ── 알림 (텔레그램 · 카카오톡) ─────────────────────────────
def _public_base() -> str:
    host = os.environ.get("WEB_PUBLIC_HOST") or request.host
    h = host.split(":")[0]
    local = h in ("localhost", "127.0.0.1") or h.replace(".", "").isdigit()
    proto = request.headers.get("X-Forwarded-Proto") or ("http" if local else "https")
    return f"{proto}://{host}"


@app.post("/schedule")
@login_required
def schedule_save():
    from trader import schedule
    text = CONFIG_PATH.read_text(encoding="utf-8")
    new, errs = {}, []
    for mk in ("kr", "us"):
        ts, err = schedule.parse(request.form.get(f"sched_{mk}", ""), mk)
        if err:
            errs.append(err)
        new[mk] = ts
    if errs:
        for e in errs:
            flash(e, "err")
        return redirect(url_for("settings") + "#schedule")
    old = settings_io.read(CONFIG_PATH)
    try:
        wm = int(request.form.get("watch_minutes", "15"))
    except ValueError:
        wm = 15
    wm = wm if wm in schedule.WATCH_CHOICES else 15
    for mk in ("kr", "us"):
        text = settings_io._set(text, "schedule", mk, new[mk])
    text = settings_io._set(text, "schedule", "watch_minutes", wm)
    CONFIG_PATH.write_text(text, encoding="utf-8")
    if schedule.watch_minutes(old) != wm or request.form.get("watch_apply"):
        flash(schedule.apply_watch(wm, ["kr"] + (["us"] if cfg().us_enabled else [])), "ok")
    for mk in ("kr", "us"):
        if schedule.times(old, mk) != new[mk]:
            name = "국내" if mk == "kr" else "미국"
            flash(f"{name} 자동 실행: {', '.join(new[mk])} — {schedule.apply(mk, new[mk])}", "ok")
    c = cfg()
    Journal(c.db_path).add_log(c.ledger, "schedule", "web", f"자동 실행 시각: 국내 {new['kr']} / 미국 {new['us']}")
    return redirect(url_for("settings") + "#schedule")


@app.post("/notify/save")
@login_required
def notify_save():
    from trader import notify
    text = CONFIG_PATH.read_text(encoding="utf-8")
    for k in notify.KINDS:
        text = settings_io._set(text, "notify", k, bool(request.form.get(f"n_{k}")))
    CONFIG_PATH.write_text(text, encoding="utf-8")
    flash("알림 설정을 저장했습니다.", "ok")
    return redirect(url_for("settings") + "#notify")


@app.post("/notify/test")
@login_required
def notify_test():
    from trader import notify
    c = cfg()
    chs = notify.channels(c.data_dir)
    if not chs:
        flash("연결된 알림이 없습니다. 텔레그램 키를 넣거나 카카오톡을 연결하세요.", "err")
    else:
        errs = notify.send(c, "test", f"🔔 AI 자동매매 테스트 알림 ({datetime.now(KST):%m/%d %H:%M})", force=True)
        ok = [ch for ch in chs if not any(e.startswith(ch) for e in errs)]
        names = {"telegram": "텔레그램", "kakao": "카카오톡"}
        if ok:
            flash("보냈습니다: " + ", ".join(names[x] for x in ok), "ok")
        for e in errs:
            flash("실패 — " + e, "err")
    return redirect(url_for("settings") + "#notify")


@app.post("/notify/telegram/find")
@login_required
def telegram_find():
    from trader import envfile, notify
    try:
        chats = notify.telegram_find_chat()
    except Exception as e:  # noqa: BLE001
        flash(f"텔레그램 조회 실패: {e}", "err")
        return redirect(url_for("settings") + "#notify")
    if not chats:
        flash("대화를 찾지 못했어요. 텔레그램에서 봇에게 아무 메시지나 보낸 뒤 다시 누르세요.", "err")
    elif len(chats) == 1:
        envfile.update({"TELEGRAM_CHAT_ID": chats[0]["id"]})
        os.environ["TELEGRAM_CHAT_ID"] = chats[0]["id"]
        flash(f"채팅 ID 저장: {chats[0]['name'] or chats[0]['id']} — ‘테스트 보내기’로 확인하세요.", "ok")
    else:
        flash("대화가 여러 개예요. 연결 정보 → 알림의 채팅 ID에 직접 넣으세요: "
              + ", ".join(f"{c_['name']}={c_['id']}" for c_ in chats), "err")
    return redirect(url_for("settings") + "#notify")


@app.get("/kakao/connect")
@login_required
def kakao_connect():
    from trader import notify
    if not os.environ.get("KAKAO_REST_KEY"):
        flash("먼저 연결 정보 → 알림에 카카오 REST API 키를 넣으세요.", "err")
        return redirect(url_for("settings") + "#notify")
    return redirect(notify.kakao_auth_url(cfg().data_dir, _public_base() + url_for("kakao_callback")))


@app.get("/kakao/callback")
def kakao_callback():
    """카카오에서 돌아오는 주소 — 쿠키(SameSite=Strict)가 안 오므로 로그인 대신 일회용 state 로 확인."""
    from trader import notify
    err = request.args.get("error_description") or request.args.get("error")
    if not err:
        try:
            notify.kakao_callback(cfg().data_dir, request.args.get("code", ""), request.args.get("state", ""))
        except Exception as e:  # noqa: BLE001
            err = str(e)
    if not err:
        try:
            notify.kakao_send(cfg().data_dir, "🔔 AI 자동매매 카카오톡 알림이 연결됐어요.")
        except Exception as e:  # noqa: BLE001
            err = f"연결은 됐지만 테스트 메시지 실패: {e}"
    return render_template("kakao_done.html", err=err)


@app.post("/kakao/disconnect")
@login_required
def kakao_disconnect():
    from trader import notify
    notify.kakao_disconnect(cfg().data_dir)
    flash("카카오톡 알림 연결을 끊었습니다.", "ok")
    return redirect(url_for("settings") + "#notify")


# ── 백테스트 ───────────────────────────────────────────────
BENCH = {"kr": ("069500", "KODEX 200 (코스피200)"), "us": ("SPY", "SPY (S&P 500)")}


def _run_backtest(market: str, p: dict) -> None:
    from trader import backtest
    from trader.runner import make_broker, market_cfg
    c = market_cfg(cfg(), market)
    j = Journal(c.db_path)
    try:
        res = backtest.run_job(c, make_broker(c), p["days"], p["stop"], p["take"], p["symbols"], BENCH[market][0],
                               progress=lambda k, n, code: _job.update(progress=f"{k}/{n} {code}"))
        j.add_log(c.ledger, "backtest", "web",
                  f"백테스트 {res.get('start')}~{res.get('end')}: 수익률 {res['return_pct']:+.2f}% "
                  f"(기준 {res.get('bench_pct')}%), 최대 낙폭 {res['mdd_pct']}%, 거래 {res['n']}회 ({res['seconds']}초)")
    except Exception as e:  # noqa: BLE001
        j.add_log(c.ledger, "backtest", "web", f"❌ 백테스트 실패: {e}")


def lines_svg(series: list[list[float]], labels: list[str], classes: list[str]) -> str:
    pts_all = [v for s in series for v in s if v is not None]
    if not pts_all or max(len(s) for s in series) < 2:
        return ""
    w, h, pad = 600, 170, 6
    lo, hi = min(pts_all), max(pts_all)
    span = (hi - lo) or 1
    out = [f'<svg viewBox="0 0 {w} {h}" class="chart" role="img" aria-label="자산 비교">']
    for s, cl in zip(series, classes):
        n = len(s)
        pts = " ".join(f"{pad + i * (w - 2 * pad) / (n - 1):.1f},{h - pad - (v - lo) * (h - 2 * pad) / span:.1f}"
                       for i, v in enumerate(s) if v is not None)
        out.append(f'<polyline points="{pts}" class="line {cl}"/>')
    out.append("</svg>")
    return "".join(out)


@app.route("/backtest", methods=["GET", "POST"])
@login_required
def backtest_page():
    from trader import backtest
    m = current_market()
    base = cfg()
    c = base.for_market(m)
    if request.method == "POST":
        try:
            days = int(request.form.get("days", "500"))
            stop = float(request.form.get("stop", c.stop_loss_pct))
            take = float(request.form.get("take", c.take_profit_pct))
        except ValueError:
            flash("숫자를 확인하세요.", "err")
            return redirect(url_for("backtest_page"))
        if days not in (250, 500, 750) or not (-50 <= stop < 0) or not (0 < take <= 200):
            flash("기간·손절(-50~0)·익절(0~200)을 확인하세요.", "err")
            return redirect(url_for("backtest_page"))
        syms = [cfgmod.norm_symbol(x, m) for x in (request.form.get("symbols") or "").replace(",", " ").split()]
        syms = list(dict.fromkeys(x for x in syms if x))[:40] or c.symbols[:40]
        err = _start_job("backtest", m, {"days": days, "stop": stop, "take": take, "symbols": syms})
        flash(err or f"🧪 백테스트를 시작했어요 — {len(syms)}종목, 처음엔 데이터 받느라 1~3분 걸려요.", "err" if err else "ok")
        return redirect(url_for("backtest_page"))
    r = backtest.load(c.data_dir, m)
    chart = ""
    if r and r.get("curve"):
        eq = [x["equity"] for x in r["curve"]]
        bmap = {x["date"]: x["v"] for x in r.get("bench_curve") or []}
        chart = lines_svg([eq, [bmap.get(x["date"]) for x in r["curve"]]] if bmap else [eq],
                          ["봇 전략", "기준"], ["up" if eq[-1] >= eq[0] else "down", "bench"])
    return render_template("backtest.html", c=c, base=base, market=m, r=r, chart=chart, bench=BENCH[m],
                           busy=_busy(c), job=_job)


# ── 패스키 로그인 ─────────────────────────────────────
def _pk_ctx():
    from web import passkeys
    return passkeys.rp_and_origin(request.host, request.headers.get("Origin"))


def _json_err(msg: str, code: int = 400):
    return jsonify(error=msg), code


def _pw_ok(pw_in: str) -> bool:
    pw = os.environ.get("WEB_PASSWORD", "")
    return bool(pw) and hmac.compare_digest(pw_in or "", pw)


@app.get("/passkey/login/options")
def passkey_login_options():
    from web import passkeys
    try:
        opts, chal = passkeys.login_options(passkeys.rp_id_for(request.host))
    except Exception as e:  # noqa: BLE001
        return _json_err(str(e))
    session["pk_chal"] = chal
    return app.response_class(opts, mimetype="application/json")


@app.post("/passkey/login/verify")
def passkey_login_verify():
    from web import passkeys
    chal = session.pop("pk_chal", None)
    if not chal:
        return _json_err("다시 시도하세요 (만료)")
    try:
        rp_id, origin = _pk_ctx()
        passkeys.login_verify(cfg().data_dir, (request.get_json(silent=True) or {}).get("credential") or {},
                              chal, rp_id, origin)
    except Exception as e:  # noqa: BLE001
        time.sleep(1.0)
        return _json_err(f"패스키 로그인 실패: {e}", 403)
    session.clear()
    session["ok"] = True
    session.permanent = True
    return jsonify(ok=True)


@app.post("/passkey/register/options")
@login_required
def passkey_register_options():
    from web import passkeys
    if not _pw_ok((request.get_json(silent=True) or {}).get("password", "")):
        time.sleep(1.0)
        return _json_err("웹 비밀번호가 맞지 않습니다", 403)
    try:
        rp_id, _origin = _pk_ctx()
        opts, chal = passkeys.register_options(cfg().data_dir, rp_id)
    except Exception as e:  # noqa: BLE001
        return _json_err(str(e))
    session["pk_reg_chal"] = chal
    return app.response_class(opts, mimetype="application/json")


@app.post("/passkey/register/verify")
@login_required
def passkey_register_verify():
    from web import passkeys
    chal = session.pop("pk_reg_chal", None)
    if not chal:
        return _json_err("다시 시도하세요 (만료)")
    body = request.get_json(silent=True) or {}
    try:
        rp_id, origin = _pk_ctx()
        item = passkeys.register_verify(cfg().data_dir, body.get("credential") or {}, chal, rp_id, origin,
                                        str(body.get("name") or ""))
    except Exception as e:  # noqa: BLE001
        return _json_err(f"등록 실패: {e}")
    c = cfg()
    Journal(c.db_path).add_log(c.ledger, "passkey", "web", f"패스키 등록: {item['name']}")
    return jsonify(ok=True, name=item["name"])


@app.post("/passkey/delete")
@login_required
def passkey_delete():
    from web import passkeys
    if not _pw_ok(request.form.get("password", "")):
        time.sleep(1.0)
        flash("웹 비밀번호가 맞지 않습니다.", "err")
    elif passkeys.remove(cfg().data_dir, request.form.get("id", "")):
        c = cfg()
        Journal(c.db_path).add_log(c.ledger, "passkey", "web", "패스키 삭제")
        flash("삭제했습니다.", "ok")
    return redirect(url_for("settings") + "#security")


# ── 업데이트 (GitHub → 서버) ──────────────────────────────
def _web_update() -> bool:
    return os.environ.get("WEB_UPDATE", "on").strip().lower() not in ("off", "no", "0", "false")


@app.post("/update/check")
@login_required
def update_check():
    from trader import updater
    if not _web_update():
        flash("웹 업데이트가 꺼져 있습니다 (.env 의 WEB_UPDATE=off).", "err")
        return redirect(url_for("settings") + "#update")
    r = updater.check(cfg().data_dir)
    if r.get("error"):
        flash(f"업데이트 확인 실패: {r['error']}", "err")
    elif not r.get("behind"):
        flash(f"✅ 최신 버전입니다 (v{r['current']}).", "ok")
    return redirect(url_for("settings") + "#update")


@app.post("/update/apply")
@login_required
def update_apply():
    from trader import updater
    c = cfg()
    if not _web_update():
        flash("웹 업데이트가 꺼져 있습니다 (.env 의 WEB_UPDATE=off).", "err")
        return redirect(url_for("settings") + "#update")
    pw = os.environ.get("WEB_PASSWORD", "")
    if not (pw and hmac.compare_digest(request.form.get("password", ""), pw)):
        time.sleep(1.0)
        flash("비밀번호가 맞지 않아 업데이트하지 않았습니다.", "err")
        return redirect(url_for("settings") + "#update")
    if _busy(c):
        flash("매매·분석이 실행 중입니다. 끝난 뒤 다시 시도하세요.", "err")
        return redirect(url_for("settings") + "#update")
    try:
        updater.start(c.data_dir)
    except Exception as e:  # noqa: BLE001
        flash(f"업데이트를 시작하지 못했습니다: {e}", "err")
        return redirect(url_for("settings") + "#update")
    Journal(c.db_path).add_log(c.ledger, "update", "web", f"업데이트 시작 (v{_version()})")
    return redirect(url_for("update_progress"))


def _version() -> str:
    from trader import __version__
    return __version__


@app.get("/update")
@login_required
def update_progress():
    return render_template("updating.html", version=_version())


@app.get("/api/update_log")
@login_required
def api_update_log():
    from trader import updater
    text, done, code = updater.log_text(cfg().data_dir)
    return jsonify(log=text[-6000:], done=done, code=code, version=_version())


# ── 동작 ───────────────────────────────────────────────
@app.post("/action/<name>")
@login_required
def action(name):
    m = current_market()
    c = cfg()
    if name == "stop":
        c.stop_file.write_text(datetime.now(KST).isoformat())
        flash("⏸ 정지했습니다. 다음 실행부터 매매하지 않습니다.", "ok")
    elif name == "resume":
        c.stop_file.unlink(missing_ok=True)
        j = Journal(c.db_path)
        for mk in ("kr", "us"):
            j.set(f"halted_{c.for_market(mk).ledger}", "0")
        flash("▶ 재개했습니다.", "ok")
    elif name in ("analyze", "trade", "scan"):
        err = _start_job(name, "kr" if name == "scan" else m)
        msg = {"analyze": "🧠 AI 분석을 시작했습니다 (주문 없음).",
               "trade": "🚀 매매 사이클을 시작했습니다.",
               "scan": "🔎 자동 종목 검색을 시작했습니다 (1분 정도)."}[name]
        flash(err or msg, "err" if err else "ok")
    elif name == "refresh":
        try:
            refresh_balance(c, m)
            flash("잔고를 새로 불러왔습니다.", "ok")
        except Exception as e:  # noqa: BLE001
            flash(f"잔고 조회 실패: {e}", "err")
    return redirect(request.referrer or url_for("index"))


@app.get("/api/status")
@login_required
def api_status():
    c = cfg()
    logs = Journal(c.db_path).logs(1)
    return jsonify(busy=_busy(c), last_log_id=logs[0]["id"] if logs else None)


def main():
    if not os.environ.get("WEB_PASSWORD") or len(os.environ["WEB_PASSWORD"]) < 8:
        sys.exit("웹 비밀번호가 없습니다 → .venv/bin/python run.py passwd  (또는 .env 에 WEB_PASSWORD 를 8자 이상)")
    host = os.environ.get("WEB_HOST", "0.0.0.0")
    port = int(os.environ.get("WEB_PORT", "8080"))
    threading.Thread(target=_monitor_loop, daemon=True, name="monitor").start()
    from waitress import serve
    print(f"웹 관리 화면: http://{host}:{port}")
    serve(app, host=host, port=port, threads=4)


if __name__ == "__main__":
    main()
