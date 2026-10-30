"""알림 — 텔레그램 / 카카오톡(나에게 보내기).

설정
  .env      TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
            KAKAO_REST_KEY (+ 선택 KAKAO_CLIENT_SECRET) — 연결은 웹 설정 → 알림 → ‘카카오톡 연결’
  config    [notify] orders / errors / halt / daily  (true/false)

알림 실패는 매매에 영향을 주지 않는다 (로그만 남김).
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import time
from pathlib import Path

import requests

log = logging.getLogger("notify")
KINDS = {"orders": "주문 실행·실패", "errors": "실행 오류 (AI·증권사 연결 등)",
         "halt": "최대 손실 한도로 정지", "daily": "하루 마감 요약 (그날 마지막 실행 후)"}
DEFAULTS = {"orders": True, "errors": True, "halt": True, "daily": False}
TIMEOUT = 10


# ── 텔레그램 ────────────────────────────────────────────
def telegram_ready() -> bool:
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))


def telegram_send(text: str) -> None:
    tok, chat = os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
    r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                      json={"chat_id": chat, "text": text[:4000], "disable_web_page_preview": True}, timeout=TIMEOUT)
    if not r.ok:
        raise RuntimeError(f"텔레그램 {r.status_code}: {r.text[:200]}")


def telegram_find_chat() -> list[dict]:
    """봇에게 아무 메시지나 보낸 뒤 호출 → 최근 대화의 chat id 목록."""
    tok = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not tok:
        raise RuntimeError("TELEGRAM_BOT_TOKEN 이 없습니다")
    r = requests.get(f"https://api.telegram.org/bot{tok}/getUpdates", timeout=TIMEOUT)
    if not r.ok:
        raise RuntimeError(f"텔레그램 {r.status_code}: {r.text[:200]}")
    seen = {}
    for u in r.json().get("result", []):
        ch = (u.get("message") or u.get("channel_post") or {}).get("chat") or {}
        if ch.get("id") is not None:
            seen[ch["id"]] = {"id": str(ch["id"]),
                              "name": ch.get("title") or " ".join(x for x in (ch.get("first_name"), ch.get("last_name")) if x)
                              or ch.get("username") or ""}
    return list(seen.values())


# ── 카카오톡 (나에게 보내기) ─────────────────────────────
def _kakao_file(data_dir: Path) -> Path:
    return Path(data_dir) / "kakao_token.json"


def kakao_ready(data_dir: Path) -> bool:
    return bool(os.environ.get("KAKAO_REST_KEY")) and _kakao_file(data_dir).exists()


def kakao_auth_url(data_dir: Path, redirect_uri: str) -> str:
    state = secrets.token_urlsafe(24)
    (Path(data_dir) / "kakao_state.json").write_text(json.dumps({"state": state, "exp": time.time() + 600,
                                                                 "redirect_uri": redirect_uri}))
    from urllib.parse import urlencode
    return "https://kauth.kakao.com/oauth/authorize?" + urlencode({
        "client_id": os.environ["KAKAO_REST_KEY"], "redirect_uri": redirect_uri, "response_type": "code",
        "scope": "talk_message", "state": state})


def kakao_callback(data_dir: Path, code: str, state: str) -> None:
    p = Path(data_dir) / "kakao_state.json"
    try:
        st = json.loads(p.read_text())
    except (OSError, ValueError):
        raise RuntimeError("연결 요청 정보가 없습니다. 웹 설정에서 다시 시도하세요") from None
    p.unlink(missing_ok=True)
    if not secrets.compare_digest(st.get("state", ""), state or "") or st.get("exp", 0) < time.time():
        raise RuntimeError("연결 요청이 만료됐거나 맞지 않습니다. 다시 시도하세요")
    data = {"grant_type": "authorization_code", "client_id": os.environ["KAKAO_REST_KEY"],
            "redirect_uri": st["redirect_uri"], "code": code}
    if os.environ.get("KAKAO_CLIENT_SECRET"):
        data["client_secret"] = os.environ["KAKAO_CLIENT_SECRET"]
    r = requests.post("https://kauth.kakao.com/oauth/token", data=data, timeout=TIMEOUT)
    if not r.ok:
        raise RuntimeError(f"카카오 토큰 발급 실패 {r.status_code}: {r.text[:200]}")
    _kakao_save(data_dir, r.json(), link=st["redirect_uri"].rsplit("/kakao/", 1)[0])


def _kakao_save(data_dir: Path, tok: dict, link: str | None = None) -> None:
    f = _kakao_file(data_dir)
    old = {}
    if f.exists():
        try:
            old = json.loads(f.read_text())
        except ValueError:
            pass
    now = time.time()
    new = {**old,
           "access_token": tok["access_token"], "access_exp": now + int(tok.get("expires_in", 21599)) - 60,
           "link": link or old.get("link", "")}
    if tok.get("refresh_token"):
        new["refresh_token"] = tok["refresh_token"]
        new["refresh_exp"] = now + int(tok.get("refresh_token_expires_in", 5184000))
    tmp = f.with_name(f.name + ".tmp")
    tmp.write_text(json.dumps(new))
    os.chmod(tmp, 0o600)
    tmp.replace(f)


def kakao_info(data_dir: Path) -> dict | None:
    try:
        t = json.loads(_kakao_file(data_dir).read_text())
    except (OSError, ValueError):
        return None
    return {"refresh_days": max(0, int((t.get("refresh_exp", 0) - time.time()) / 86400))}


def _kakao_token(data_dir: Path) -> dict:
    t = json.loads(_kakao_file(data_dir).read_text())
    if t.get("access_exp", 0) > time.time():
        return t
    if t.get("refresh_exp", 0) < time.time():
        raise RuntimeError("카카오 연결이 만료됐습니다 (2개월). 웹 설정 → 알림에서 다시 연결하세요")
    data = {"grant_type": "refresh_token", "client_id": os.environ["KAKAO_REST_KEY"],
            "refresh_token": t["refresh_token"]}
    if os.environ.get("KAKAO_CLIENT_SECRET"):
        data["client_secret"] = os.environ["KAKAO_CLIENT_SECRET"]
    r = requests.post("https://kauth.kakao.com/oauth/token", data=data, timeout=TIMEOUT)
    if not r.ok:
        raise RuntimeError(f"카카오 토큰 갱신 실패 {r.status_code}: {r.text[:200]}")
    _kakao_save(data_dir, r.json())       # 만료 1달 이내면 새 refresh_token 도 같이 옴
    return json.loads(_kakao_file(data_dir).read_text())


def kakao_send(data_dir: Path, text: str) -> None:
    t = _kakao_token(data_dir)
    link = t.get("link") or "https://localhost"
    tpl = {"object_type": "text", "text": text[:190] + ("…" if len(text) > 190 else ""),
           "link": {"web_url": link, "mobile_web_url": link}, "button_title": "웹에서 보기"}
    r = requests.post("https://kapi.kakao.com/v2/api/talk/memo/default/send",
                      headers={"Authorization": f"Bearer {t['access_token']}"},
                      data={"template_object": json.dumps(tpl, ensure_ascii=False)}, timeout=TIMEOUT)
    if not r.ok:
        raise RuntimeError(f"카카오톡 {r.status_code}: {r.text[:200]}")


def kakao_disconnect(data_dir: Path) -> None:
    _kakao_file(data_dir).unlink(missing_ok=True)


# ── 공통 ───────────────────────────────────────────────
def channels(data_dir: Path) -> list[str]:
    out = []
    if telegram_ready():
        out.append("telegram")
    if kakao_ready(data_dir):
        out.append("kakao")
    return out


def enabled(cfg, kind: str) -> bool:
    return bool((getattr(cfg, "notify", None) or {}).get(kind, DEFAULTS.get(kind, False)))


def send(cfg, kind: str, text: str, force: bool = False) -> list[str]:
    """설정에서 켠 종류만 보낸다. 반환: 실패 메시지 목록. 예외를 던지지 않는다."""
    if not force and not enabled(cfg, kind):
        return []
    errs = []
    for ch in channels(cfg.data_dir):
        try:
            telegram_send(text) if ch == "telegram" else kakao_send(cfg.data_dir, text)
        except Exception as e:  # noqa: BLE001
            log.warning("알림 실패 (%s): %s", ch, e)
            errs.append(f"{ch}: {e}")
    return errs
