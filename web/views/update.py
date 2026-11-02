"""웹 화면 — 업데이트 (GitHub → 서버)."""

import hmac
import os
import time

from flask import flash, jsonify, redirect, render_template, request, url_for

from trader.journal import Journal  # noqa: E402
from web import app as webapp  # noqa: E402,F401
from web.app import app, login_required  # noqa: E402,F401


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
    r = updater.check(webapp.cfg().data_dir)
    if r.get("error"):
        flash(f"업데이트 확인 실패: {r['error']}", "err")
    elif not r.get("behind"):
        flash(f"✅ 최신 버전입니다 (v{r['current']}).", "ok")
    return redirect(url_for("settings") + "#update")


@app.post("/update/apply")
@login_required
def update_apply():
    from trader import updater
    c = webapp.cfg()
    if not _web_update():
        flash("웹 업데이트가 꺼져 있습니다 (.env 의 WEB_UPDATE=off).", "err")
        return redirect(url_for("settings") + "#update")
    pw = os.environ.get("WEB_PASSWORD", "")
    if not (pw and hmac.compare_digest(request.form.get("password", ""), pw)):
        time.sleep(1.0)
        flash("비밀번호가 맞지 않아 업데이트하지 않았습니다.", "err")
        return redirect(url_for("settings") + "#update")
    if webapp._busy(c):
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
    text, done, code = updater.log_text(webapp.cfg().data_dir)
    return jsonify(log=text[-6000:], done=done, code=code, version=_version())
