"""AI 자동매매 웹 관리 화면.

  .venv/bin/python -m web.app          # http://<LXC IP>:8080
  .env 의 WEB_PASSWORD 로 로그인합니다.

보안상 웹에서 할 수 없는 일: 앱키·계좌번호 변경 (서버의 .env 에서만).
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
    return render_template("login.html")


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


def _check_mode(c, new: str) -> tuple[list[str], list[str]]:
    """새 모드로 증권사에 실제로 붙어 보고 (오류, 결과 요약) 반환. 실패하면 전환하지 않는다."""
    from dataclasses import replace
    from trader.runner import broker_name, make_broker
    errs, info = [], []
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
        except Exception as e:  # noqa: BLE001
            errs.append(f"{flag} {name} {MODE_LABEL[new]} 연결 실패: {e}")
    return errs, info


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
    errs, info = _check_mode(c, new)
    if errs:
        for e in errs:
            flash(e, "err")
        flash(f"{MODE_LABEL[new]}(으)로 바꾸지 않았습니다.", "err")
        return redirect(url_for("settings"))
    old = c.mode
    modefile.write(c.data_dir, new, live_ack=(new == "live"))
    msg = f"실행 모드: {MODE_LABEL[old]} → {MODE_LABEL[new]}" + ("".join("\n" + x for x in info))
    Journal(c.db_path).add_log(new, "mode", "web", msg)
    flash(("🔴 " if new == "live" else "") + msg.replace("\n", " · ") + " — 다음 실행부터 적용됩니다.", "ok")
    return redirect(url_for("settings"))


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
        sys.exit(".env 에 WEB_PASSWORD 를 8자 이상으로 설정하세요.")
    host = os.environ.get("WEB_HOST", "0.0.0.0")
    port = int(os.environ.get("WEB_PORT", "8080"))
    from waitress import serve
    print(f"웹 관리 화면: http://{host}:{port}")
    serve(app, host=host, port=port, threads=4)


if __name__ == "__main__":
    main()
