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
SCHEDULE = [(10, 0), (14, 30)]  # deploy/ai-trader.timer 와 동일


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


def _start_job(kind: str, market: str = "kr") -> str | None:
    c = cfg()
    with _job_lock:
        if _busy(c):
            return "이미 실행 중입니다."
        _job.update(running=True, kind=kind, started=time.time())

    def work():
        try:
            if kind == "scan":
                run_scan(cfg(), source="web")
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
US_SCHEDULE = [(10, 30), (15, 0)]  # deploy/ai-trader-us.timer 와 동일 (뉴욕 시간)
ET = ZoneInfo("America/New_York")


def next_run_us(now: datetime) -> datetime:
    n = now.astimezone(ET)
    for d in range(8):
        day = n.date() + timedelta(days=d)
        if day.weekday() >= 5:
            continue
        for h, m in US_SCHEDULE:
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
        for h, m in SCHEDULE:
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
    from trader.runner import broker_name
    return render_template(
        "index.html", c=c, base=base, market=m, broker=broker_name(c),
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
    r = ledger.period(j, c.ledger, start, end)
    days = r["by_day"]
    monthly = len(days) > 45
    if monthly:
        agg: dict[str, float] = {}
        for d, v in days:
            agg[d[:7] + "-01"] = agg.get(d[:7] + "-01", 0) + v
        days = sorted(agg.items())
    return render_template("pnl.html", c=c, base=base, market=m, r=r, periods=PERIODS, p=p,
                           start=start, end=end, f=f or "", t=t or "", chart=bars_svg(days), monthly=monthly,
                           snap=j.snapshot(c.ledger))


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
    fields = [(key, label, raw.get(sec, {}).get(key, "")) for sec, key, _t, _lo, _hi, label in settings_io.FIELDS]
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
                           notify_kinds=__import__("trader.notify", fromlist=["x"]).KINDS,
                           notify_on={k: __import__("trader.notify", fromlist=["x"]).enabled(cfg(), k)
                                      for k in __import__("trader.notify", fromlist=["x"]).KINDS},
                           tg_ready=__import__("trader.notify", fromlist=["x"]).telegram_ready(),
                           tg_token=bool(os.environ.get("TELEGRAM_BOT_TOKEN")),
                           kakao_key=bool(os.environ.get("KAKAO_REST_KEY")),
                           kakao=__import__("trader.notify", fromlist=["x"]).kakao_info(cfg().data_dir),
                           kakao_redirect=_public_base() + url_for("kakao_callback"),
                           passkey_list=__import__("web.passkeys", fromlist=["x"]).load(cfg().data_dir),
                           acct_choice=session.get("acct_choice"),
                           mode_label=MODE_LABEL, live_phrase=LIVE_PHRASE, web_live=_web_live_switch(),
                           env_mode=os.environ.get("TRADING_MODE", "dry").strip().lower(), raw=raw, auto_fields=auto_fields,
                           us_fields=us_fields, us=us_raw, us_broker=us_broker,
                           order_types=settings_io.ORDER_TYPES, providers=settings_io.PROVIDER_CHOICES,
                           model_fields=settings_io.MODEL_FIELDS,
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
    ]),
    ("알림 — 텔레그램 · 카카오톡 (선택)", "notify", [
        ("TELEGRAM_BOT_TOKEN", "텔레그램 봇 토큰 (@BotFather)", "secret"),
        ("TELEGRAM_CHAT_ID", "텔레그램 채팅 ID — 알림 칸의 ‘찾기’로 자동 입력", "plain"),
        ("KAKAO_REST_KEY", "카카오 REST API 키 (developers.kakao.com)", "id"),
        ("KAKAO_CLIENT_SECRET", "카카오 Client Secret — 켠 경우만", "secret"),
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


# ── 알림 (텔레그램 · 카카오톡) ─────────────────────────────
def _public_base() -> str:
    host = os.environ.get("WEB_PUBLIC_HOST") or request.host
    h = host.split(":")[0]
    local = h in ("localhost", "127.0.0.1") or h.replace(".", "").isdigit()
    proto = request.headers.get("X-Forwarded-Proto") or ("http" if local else "https")
    return f"{proto}://{host}"


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
    from waitress import serve
    print(f"웹 관리 화면: http://{host}:{port}")
    serve(app, host=host, port=port, threads=4)


if __name__ == "__main__":
    main()
