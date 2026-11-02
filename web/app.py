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
if __name__ == "__main__":                       # python -m web.app — 기능별 화면이 같은 app 을 쓰게
    sys.modules.setdefault("web.app", sys.modules[__name__])


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


def _busy_info(c) -> dict | None:
    """지금 실행 중인 것: 무엇·몇 분째·지금 단계·멈춘 듯한지. 아무것도 없으면 None."""
    from trader import runstatus
    if not _busy(c):
        return None
    st = runstatus.read(c.data_dir)
    now_ = time.time()
    if _job.get("running") and _job.get("kind") == "backtest":
        started = _job.get("started") or now_
        return {"what": "백테스트", "market": "", "step": _job.get("progress", ""), "min": int((now_ - started) // 60),
                "stale": False, "stoppable": False}
    if st and not st.get("done") and runstatus.alive(st.get("pid")):
        mins = int((now_ - (st.get("started") or now_)) // 60)
        idle = int((now_ - (st.get("step_ts") or now_)) // 60)
        return {"what": st.get("what", "실행"), "market": st.get("market", ""), "step": st.get("step", ""),
                "min": mins, "idle": idle, "stale": mins >= runstatus.STALE_MIN, "stoppable": mins >= runstatus.STALE_MIN,
                "inproc": st.get("pid") == os.getpid()}
    started = _job.get("started") or now_
    mins = int((now_ - started) // 60)
    return {"what": {"trade": "매매 실행", "analyze": "AI 분석", "scan": "종목 찾기"}.get(_job.get("kind"), "실행"),
            "market": "", "step": "", "min": mins, "stale": mins >= runstatus.STALE_MIN,
            "stoppable": mins >= runstatus.STALE_MIN, "inproc": bool(_job.get("running"))}


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
                from web.views.backtest import _run_backtest
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
        snap=snap, stopped=stopped, halted=halted, busy=_busy_info(c), job=_job,
        next_run=fmt_kst(next_run(now) if m == "kr" else next_run_us(now)),
        chart=equity_svg(j.equity_series(c.ledger), c.total_krw),
        runs=j.runs_with_decisions(c.ledger, 15), orders=j.orders(c.ledger, 10),
        logs=[l for l in j.logs(40) if l.get("mode") in (c.ledger, None, "") or l.get("kind") in ("update", "mode", "env")][:8], last_log_id=(j.logs(1) or [{}])[0].get("id"),
        orders_today=j.orders_today(c.ledger),
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




def _restart_soon():
    """systemd 가 다시 띄우도록 잠시 뒤 종료 → 바뀐 .env 를 웹·증권사 모듈 모두 새로 읽는다."""
    if app.config.get("TESTING"):
        return
    threading.Timer(1.5, lambda: os._exit(3)).start()


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
    info = _busy_info(c)
    return jsonify(busy=bool(info), info=info, last_log_id=logs[0]["id"] if logs else None)


@app.post("/run/stop")
@login_required
def run_stop():
    """15분 넘게 끝나지 않는 실행 정리 — 봇 프로세스(run.py)만 멈추고, 웹 안에서 돌던 건 웹을 다시 시작해 끊는다."""
    import signal
    from trader import runstatus
    c = cfg()
    info = _busy_info(c)
    if not info:
        flash("실행 중인 작업이 없어요.", "ok")
        return redirect(url_for("index"))
    if not info.get("stoppable"):
        flash(f"아직 {runstatus.STALE_MIN}분이 안 됐어요 — 조금 더 기다려 주세요.", "err")
        return redirect(url_for("index"))
    st = runstatus.read(c.data_dir)
    pid = st.get("pid")
    j = Journal(c.db_path)
    msg = f"⛔ 멈춘 실행 정리: {info['what']} ({info['min']}분째, 마지막 단계: {info.get('step') or '-'})"
    if info.get("inproc") or pid == os.getpid():
        j.add_log(c.ledger, "stop", "web", msg + " — 웹을 다시 시작해 끊음")
        _job.update(running=False)
        _restart_soon()
        return render_template("restarting.html", title="멈춘 실행을 정리하는 중")
    if pid and runstatus.alive(pid) and runstatus.is_trader_process(pid):
        try:
            os.kill(int(pid), signal.SIGTERM)
        except OSError as e:
            flash(f"멈추지 못했어요: {e}", "err")
            return redirect(url_for("index"))
        j.add_log(c.ledger, "stop", "web", msg + f" — 프로세스 {pid} 종료")
        flash("멈춘 실행을 정리했어요. 기록 → 실행 로그에서 어디서 멈췄는지 볼 수 있어요.", "ok")
    else:
        flash("정리할 봇 프로세스를 찾지 못했어요. 서버에서 확인이 필요해요.", "err")
    return redirect(url_for("index"))


def main():
    if not os.environ.get("WEB_PASSWORD") or len(os.environ["WEB_PASSWORD"]) < 8:
        sys.exit("웹 비밀번호가 없습니다 → .venv/bin/python run.py passwd  (또는 .env 에 WEB_PASSWORD 를 8자 이상)")
    host = os.environ.get("WEB_HOST", "0.0.0.0")
    port = int(os.environ.get("WEB_PORT", "8080"))
    threading.Thread(target=_monitor_loop, daemon=True, name="monitor").start()
    from waitress import serve
    print(f"웹 관리 화면: http://{host}:{port}")
    serve(app, host=host, port=port, threads=4)


# 기능별 화면 (같은 app 에 경로를 등록 — url_for 이름은 그대로)
import importlib  # noqa: E402

for _m in ("pnl", "settings", "backup", "notify", "backtest", "passkey", "update"):
    importlib.import_module(f"web.views.{_m}")


if __name__ == "__main__":
    main()