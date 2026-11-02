"""웹 화면 — 실행 시각 · 알림(텔레그램 · 카카오톡)."""

import os
from datetime import datetime

from flask import flash, redirect, render_template, request, url_for

from trader.journal import KST, Journal  # noqa: E402
from web import settings_io  # noqa: E402
from web import app as webapp  # noqa: E402,F401
from web.app import app, login_required  # noqa: E402,F401


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
    text = webapp.CONFIG_PATH.read_text(encoding="utf-8")
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
    old = settings_io.read(webapp.CONFIG_PATH)
    try:
        wm = int(request.form.get("watch_minutes", "15"))
    except ValueError:
        wm = 15
    wm = wm if wm in schedule.WATCH_CHOICES else 15
    for mk in ("kr", "us"):
        text = settings_io._set(text, "schedule", mk, new[mk])
    text = settings_io._set(text, "schedule", "watch_minutes", wm)
    webapp.CONFIG_PATH.write_text(text, encoding="utf-8")
    if schedule.watch_minutes(old) != wm or request.form.get("watch_apply"):
        flash(schedule.apply_watch(wm, ["kr"] + (["us"] if webapp.cfg().us_enabled else [])), "ok")
    for mk in ("kr", "us"):
        if schedule.times(old, mk) != new[mk]:
            name = "국내" if mk == "kr" else "미국"
            flash(f"{name} 자동 실행: {', '.join(new[mk])} — {schedule.apply(mk, new[mk])}", "ok")
    c = webapp.cfg()
    Journal(c.db_path).add_log(c.ledger, "schedule", "web", f"자동 실행 시각: 국내 {new['kr']} / 미국 {new['us']}")
    return redirect(url_for("settings") + "#schedule")


@app.post("/notify/save")
@login_required
def notify_save():
    from trader import notify
    text = webapp.CONFIG_PATH.read_text(encoding="utf-8")
    for k in notify.KINDS:
        text = settings_io._set(text, "notify", k, bool(request.form.get(f"n_{k}")))
    webapp.CONFIG_PATH.write_text(text, encoding="utf-8")
    flash("알림 설정을 저장했습니다.", "ok")
    return redirect(url_for("settings") + "#notify")


@app.post("/notify/test")
@login_required
def notify_test():
    from trader import notify
    c = webapp.cfg()
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
    return redirect(notify.kakao_auth_url(webapp.cfg().data_dir, _public_base() + url_for("kakao_callback")))


@app.get("/kakao/callback")
def kakao_callback():
    """카카오에서 돌아오는 주소 — 쿠키(SameSite=Strict)가 안 오므로 로그인 대신 일회용 state 로 확인."""
    from trader import notify
    err = request.args.get("error_description") or request.args.get("error")
    if not err:
        try:
            notify.kakao_callback(webapp.cfg().data_dir, request.args.get("code", ""), request.args.get("state", ""))
        except Exception as e:  # noqa: BLE001
            err = str(e)
    if not err:
        try:
            notify.kakao_send(webapp.cfg().data_dir, "🔔 AI 자동매매 카카오톡 알림이 연결됐어요.")
        except Exception as e:  # noqa: BLE001
            err = f"연결은 됐지만 테스트 메시지 실패: {e}"
    return render_template("kakao_done.html", err=err)


@app.post("/kakao/disconnect")
@login_required
def kakao_disconnect():
    from trader import notify
    notify.kakao_disconnect(webapp.cfg().data_dir)
    flash("카카오톡 알림 연결을 끊었습니다.", "ok")
    return redirect(url_for("settings") + "#notify")
