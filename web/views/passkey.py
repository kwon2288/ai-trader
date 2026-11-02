"""웹 화면 — 패스키 로그인."""

import hmac
import os
import time

from flask import flash, jsonify, redirect, request, session, url_for

from trader.journal import Journal  # noqa: E402
from web import app as webapp  # noqa: E402,F401
from web.app import app, login_required  # noqa: E402,F401


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
        passkeys.login_verify(webapp.cfg().data_dir, (request.get_json(silent=True) or {}).get("credential") or {},
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
        opts, chal = passkeys.register_options(webapp.cfg().data_dir, rp_id)
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
        item = passkeys.register_verify(webapp.cfg().data_dir, body.get("credential") or {}, chal, rp_id, origin,
                                        str(body.get("name") or ""))
    except Exception as e:  # noqa: BLE001
        return _json_err(f"등록 실패: {e}")
    c = webapp.cfg()
    Journal(c.db_path).add_log(c.ledger, "passkey", "web", f"패스키 등록: {item['name']}")
    return jsonify(ok=True, name=item["name"])


@app.post("/passkey/delete")
@login_required
def passkey_delete():
    from web import passkeys
    if not _pw_ok(request.form.get("password", "")):
        time.sleep(1.0)
        flash("웹 비밀번호가 맞지 않습니다.", "err")
    elif passkeys.remove(webapp.cfg().data_dir, request.form.get("id", "")):
        c = webapp.cfg()
        Journal(c.db_path).add_log(c.ledger, "passkey", "web", "패스키 삭제")
        flash("삭제했습니다.", "ok")
    return redirect(url_for("settings") + "#security")
