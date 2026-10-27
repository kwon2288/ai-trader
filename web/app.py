"""AI 자동매매 웹 관리 화면.

  .venv/bin/python -m web.app          # http://<LXC IP>:8080
  .env 의 WEB_PASSWORD 로 로그인합니다.

보안상 웹에서 할 수 없는 일: 실계좌 전환, 앱키 변경 (서버의 .env 에서만).
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
from functools import wraps
from pathlib import Path

from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trader import config as cfgmod  # noqa: E402
from trader.journal import KST, Journal  # noqa: E402
from trader.runner import Busy, refresh_balance, run_cycle  # noqa: E402
from web import settings_io  # noqa: E402

app = Flask(__name__)
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


def _start_job(kind: str) -> str | None:
    c = cfg()
    with _job_lock:
        if _busy(c):
            return "이미 실행 중입니다."
        _job.update(running=True, kind=kind, started=time.time())

    def work():
        try:
            run_cycle(cfg(), analyze_only=(kind == "analyze"), force=False, source="web")
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
    c = cfg()
    j = Journal(c.db_path)
    snap = j.snapshot(c.mode)
    stopped = c.stop_file.exists()
    halted = j.get(f"halted_{c.mode}") == "1"
    now = datetime.now(KST)
    return render_template(
        "index.html", c=c, snap=snap, stopped=stopped, halted=halted,
        busy=_busy(c), job=_job,
        next_run=(lambda t: t.strftime('%m/%d') + '(' + '월화수목금토일'[t.weekday()] + ') ' + t.strftime('%H:%M'))(next_run(now)),
        chart=equity_svg(j.equity_series(c.mode), c.total_krw),
        runs=j.runs_with_decisions(c.mode, 5), orders=j.orders(c.mode, 10),
        logs=j.logs(5), orders_today=j.orders_today(c.mode),
    )


@app.get("/history")
@login_required
def history():
    c = cfg()
    j = Journal(c.db_path)
    return render_template("history.html", c=c, runs=j.runs_with_decisions(c.mode, 30),
                           orders=j.orders(c.mode, 200), logs=j.logs(50))


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
    from trader.analyst import PROVIDERS, available
    return render_template("settings.html", c=cfg(), fields=fields, raw=raw,
                           order_types=settings_io.ORDER_TYPES, providers=settings_io.PROVIDER_CHOICES,
                           model_fields=settings_io.MODEL_FIELDS,
                           keys={p: available(p) for p in PROVIDERS},
                           key_labels={p: i["label"] for p, i in PROVIDERS.items()})


# ── 동작 ───────────────────────────────────────────────
@app.post("/action/<name>")
@login_required
def action(name):
    c = cfg()
    if name == "stop":
        c.stop_file.write_text(datetime.now(KST).isoformat())
        flash("⏸ 정지했습니다. 다음 실행부터 매매하지 않습니다.", "ok")
    elif name == "resume":
        c.stop_file.unlink(missing_ok=True)
        Journal(c.db_path).set(f"halted_{c.mode}", "0")
        flash("▶ 재개했습니다.", "ok")
    elif name in ("analyze", "trade"):
        err = _start_job(name)
        flash(err or ("🧠 AI 분석을 시작했습니다 (주문 없음)." if name == "analyze"
                      else "🚀 매매 사이클을 시작했습니다."), "err" if err else "ok")
    elif name == "refresh":
        try:
            refresh_balance(c)
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
