"""웹 화면 — 설정 · 실행 모드 · 연결 정보(.env)."""

import hmac
import os
import time

from flask import flash, redirect, render_template, request, session, url_for

from trader.journal import Journal  # noqa: E402
from web import settings_io  # noqa: E402
from web import app as webapp  # noqa: E402,F401
from web.app import app, login_required  # noqa: E402,F401
from web.views.notify import _public_base  # noqa: E402
from web.views.update import _web_update  # noqa: E402


@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    if request.method == "POST":
        errors = settings_io.validate_and_write(webapp.CONFIG_PATH, request.form)
        if errors:
            for e in errors:
                flash(e, "err")
        else:
            flash("저장했습니다. 다음 실행부터 적용됩니다.", "ok")
            return redirect(url_for("settings"))
    raw = settings_io.read(webapp.CONFIG_PATH)
    from trader.ledger import DEFAULT_COSTS
    fields = [(key, label, raw.get(sec, {}).get(key, DEFAULT_COSTS.get(key, settings_io.DEFAULTS.get(key, ""))))
              for sec, key, _t, _lo, _hi, label in settings_io.FIELDS]
    auto_fields = [(key, label, raw.get(sec, {}).get(key, settings_io.DEFAULTS.get(key, "")))
                   for sec, key, _t, _lo, _hi, label in settings_io.AUTO_FIELDS]
    us_fields = [(key, label, raw.get("us", {}).get(key, settings_io.US_DEFAULTS.get(key, "")))
                 for _sec, key, _t, _lo, _hi, label in settings_io.US_FIELDS]
    us_raw = {"enabled": False, "margin_currency": "krw", "symbols": [], **raw.get("us", {})}
    from trader.runner import broker_name
    us_broker = broker_name(webapp.cfg().for_market("us"))
    from trader.analyst import PROVIDERS, available
    return render_template("settings.html", c=webapp.cfg(), fields=fields,
                           env_groups=_env_view(), web_env=_web_env_edit(),
                           upd=__import__("trader.updater", fromlist=["x"]).last_check(webapp.cfg().data_dir),
                           web_update=_web_update(),
                           sched={mk: ", ".join(__import__("trader.schedule", fromlist=["x"]).times(raw, mk))
                                  for mk in ("kr", "us")},
                           watch_min=__import__("trader.schedule", fromlist=["x"]).watch_minutes(raw),
                           notify_kinds=__import__("trader.notify", fromlist=["x"]).KINDS,
                           notify_on={k: __import__("trader.notify", fromlist=["x"]).enabled(webapp.cfg(), k)
                                      for k in __import__("trader.notify", fromlist=["x"]).KINDS},
                           tg_ready=__import__("trader.notify", fromlist=["x"]).telegram_ready(),
                           tg_token=bool(os.environ.get("TELEGRAM_BOT_TOKEN")),
                           kakao_key=bool(os.environ.get("KAKAO_REST_KEY")),
                           kakao=__import__("trader.notify", fromlist=["x"]).kakao_info(webapp.cfg().data_dir),
                           kakao_redirect=_public_base() + url_for("kakao_callback"),
                           backups=__import__("trader.backup", fromlist=["x"]).listing(webapp.cfg().data_dir),
                           strat=raw.get("strategy", {}), strat_m=_strategy_by_market(),
                           bk=__import__("trader.backup", fromlist=["x"]).settings(raw),
                           rmt=__import__("trader.remote", fromlist=["x"]).settings(raw),
                           rmt_types=__import__("trader.remote", fromlist=["x"]).TYPES,
                           passkey_list=__import__("web.passkeys", fromlist=["x"]).load(webapp.cfg().data_dir),
                           acct_choice=session.get("acct_choice"),
                           mode_label=MODE_LABEL, live_phrase=LIVE_PHRASE, web_live=_web_live_switch(),
                           env_mode=os.environ.get("TRADING_MODE", "dry").strip().lower(), raw=raw, auto_fields=auto_fields,
                           us_fields=us_fields, us=us_raw, us_broker=us_broker,
                           order_types=settings_io.ORDER_TYPES, providers=settings_io.PROVIDER_CHOICES,
                           model_fields=__import__("trader.pricing", fromlist=["x"]).model_options(
                               settings_io.MODEL_FIELDS, __import__("trader.pricing", fromlist=["x"]).settings(webapp.cfg().data_dir)),
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
    c = webapp.cfg()
    new = request.form.get("mode", "")
    if new == "env":                                   # 웹 설정을 지우고 .env 값으로
        modefile.clear(c.data_dir)
        flash(f"웹 모드 설정을 지웠습니다. 이제 .env 의 TRADING_MODE 를 따릅니다 (현재 {webapp.cfg().mode}).", "ok")
        return redirect(url_for("settings"))
    if new not in modefile.MODES:
        flash("알 수 없는 모드입니다.", "err")
        return redirect(url_for("settings"))
    if webapp._busy(c):
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


@app.post("/env")
@login_required
def change_env():
    from trader import envfile
    c = webapp.cfg()
    if not _web_env_edit():
        flash("웹에서 연결 정보 변경이 꺼져 있습니다 (.env 의 WEB_ENV_EDIT=off).", "err")
        return redirect(url_for("settings"))
    pw = os.environ.get("WEB_PASSWORD", "")
    if not (pw and hmac.compare_digest(request.form.get("current_password", ""), pw)):
        time.sleep(1.0)
        flash("현재 웹 비밀번호가 맞지 않아 저장하지 않았습니다.", "err")
        return redirect(url_for("settings") + "#env")
    if webapp._busy(c):
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
    webapp._restart_soon()
    return render_template("restarting.html", names=names)


def _strategy_by_market() -> dict:
    """시장별 실제 적용 값 (공통 + 시장별 덮어쓰기)."""
    from trader import strategy
    base = webapp.cfg()
    out = {}
    for mk in ("kr", "us"):
        c = base.for_market(mk)
        out[mk] = {**strategy.settings(c), "stop": c.stop_loss_pct, "take": c.take_profit_pct, "min_hold": c.min_hold_days}
    return out
