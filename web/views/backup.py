"""웹 화면 — 백업 · 복구."""

import os
import time

from flask import flash, jsonify, redirect, render_template, request, url_for

from trader.journal import Journal  # noqa: E402
from web import settings_io  # noqa: E402
from web import app as webapp  # noqa: E402,F401
from web.app import app, login_required  # noqa: E402,F401
from web.views.passkey import _pw_ok  # noqa: E402


# ── 백업 ─────────────────────────────────────────────────
@app.post("/backup/now")
@login_required
def backup_now():
    from trader import backup
    c = webapp.cfg()
    try:
        p, notes = backup.make(c.data_dir, settings_io.read(webapp.CONFIG_PATH))
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
    return send_from_directory(backup.folder(webapp.cfg().data_dir), name, as_attachment=True)


RESTORE_PHRASE = "복구"


@app.post("/backup/restore")
@login_required
def backup_restore():
    """서버의 백업 목록에서 고르거나 zip 을 올려 되돌린다 (비밀번호 + 확인 문구, 직전 상태 자동 백업)."""
    import re as _re
    from trader import backup
    c = webapp.cfg()
    back = redirect(url_for("settings") + "#backup")
    if not _pw_ok(request.form.get("password", "")):
        time.sleep(1.0)
        flash("웹 비밀번호가 맞지 않습니다.", "err")
        return back
    if request.form.get("phrase", "").strip() != RESTORE_PHRASE:
        flash(f"확인 문구를 정확히 입력하세요: {RESTORE_PHRASE}", "err")
        return back
    if webapp._busy(c):
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
            src = remote.download(settings_io.read(webapp.CONFIG_PATH), name[7:], tmp)
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
        done, before = backup.restore(c.data_dir, src, settings_io.read(webapp.CONFIG_PATH),
                                      with_env=bool(request.form.get("with_env")))
    except Exception as e:  # noqa: BLE001
        flash(f"복구하지 않았습니다: {e}", "err")
        return back
    finally:
        if tmp:
            tmp.unlink(missing_ok=True)
    Journal(webapp.cfg().db_path).add_log(c.ledger, "backup", "web",
                                   f"복구: {', '.join(done)} (주문 {info['orders']}건, 마지막 {info['last_order']}) · "
                                   f"직전 상태는 {before.name} 에 백업")
    webapp._restart_soon()
    return render_template("restarting.html", names=", ".join(done) + f" — 직전 상태는 {before.name} 로 백업해 뒀어요",
                           title="복구했습니다 ✅")


@app.get("/api/remote_backups")
@login_required
def api_remote_backups():
    from trader import remote
    try:
        return jsonify(names=remote.listing(settings_io.read(webapp.CONFIG_PATH))[:30])
    except Exception as e:  # noqa: BLE001
        return jsonify(error=str(e)[:200]), 502


@app.post("/backup/test")
@login_required
def backup_test():
    from trader import remote
    try:
        flash("🔌 " + remote.test(settings_io.read(webapp.CONFIG_PATH)), "ok")
    except Exception as e:  # noqa: BLE001
        flash(f"원격 저장 연결 실패: {e}", "err")
    return redirect(url_for("settings") + "#backup")


@app.post("/backup/settings")
@login_required
def backup_settings():
    text = webapp.CONFIG_PATH.read_text(encoding="utf-8")
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
    webapp.CONFIG_PATH.write_text(text, encoding="utf-8")
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
