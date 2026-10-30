"""패스키(Face ID / Touch ID) 로그인 — WebAuthn.

- 등록: 로그인한 상태에서 설정 → 보안 → ‘이 기기를 Face ID로 등록’ (웹 비밀번호 재입력)
- 로그인: 로그인 화면의 ‘Face ID로 로그인’
- 저장: data/passkeys.json (공개키만 저장 — 서버가 털려도 로그인 수단이 되지 않음)
- HTTPS(또는 localhost)에서만 동작. http://<IP>:8080 으로는 브라우저가 막는다.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from urllib.parse import urlparse

from webauthn import (generate_authentication_options, generate_registration_options, options_to_json,
                      verify_authentication_response, verify_registration_response)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.structs import (AuthenticatorSelectionCriteria, PublicKeyCredentialDescriptor,
                                      ResidentKeyRequirement, UserVerificationRequirement)

RP_NAME = "AI 자동매매"
USER_ID = b"ai-trader-owner"


def _file(data_dir: Path) -> Path:
    return Path(data_dir) / "passkeys.json"


def load(data_dir: Path) -> list[dict]:
    try:
        d = json.loads(_file(data_dir).read_text(encoding="utf-8"))
        return d if isinstance(d, list) else []
    except (OSError, ValueError):
        return []


def save(data_dir: Path, items: list[dict]) -> None:
    p = _file(data_dir)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(p)


def rp_id_for(host: str) -> str:
    """RP ID = 접속한 도메인. 프록시가 Host 를 바꾸면 .env 의 WEB_PUBLIC_HOST 로 지정."""
    return (os.environ.get("WEB_PUBLIC_HOST") or host).split(":")[0].strip().lower()


def rp_and_origin(host: str, origin_header: str | None) -> tuple[str, str]:
    """origin 은 브라우저가 보낸 Origin 을 쓰되 도메인이 RP ID 와 같아야 한다."""
    rp_id = rp_id_for(host)
    o = urlparse(origin_header or "")
    if not o.hostname or o.hostname.lower() != rp_id:
        raise ValueError("접속 주소가 맞지 않습니다")
    if o.scheme != "https" and rp_id not in ("localhost", "127.0.0.1"):
        raise ValueError("Face ID 로그인은 https 주소에서만 됩니다")
    return rp_id, f"{o.scheme}://{o.netloc}"


def register_options(data_dir: Path, rp_id: str) -> tuple[str, str]:
    opts = generate_registration_options(
        rp_id=rp_id, rp_name=RP_NAME, user_id=USER_ID, user_name="ai-trader", user_display_name="AI 자동매매",
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED, user_verification=UserVerificationRequirement.REQUIRED),
        exclude_credentials=[PublicKeyCredentialDescriptor(id=base64url_to_bytes(c["id"])) for c in load(data_dir)
                             if c.get("rp_id") == rp_id],
    )
    return options_to_json(opts), bytes_to_base64url(opts.challenge)


def register_verify(data_dir: Path, credential: dict, challenge: str, rp_id: str, origin: str, name: str) -> dict:
    v = verify_registration_response(credential=credential, expected_challenge=base64url_to_bytes(challenge),
                                     expected_rp_id=rp_id, expected_origin=origin, require_user_verification=True)
    items = load(data_dir)
    item = {"id": bytes_to_base64url(v.credential_id), "public_key": bytes_to_base64url(v.credential_public_key),
            "sign_count": v.sign_count, "rp_id": rp_id, "name": (name or "기기")[:40],
            "created": time.strftime("%Y-%m-%d %H:%M"), "last_used": ""}
    items = [c for c in items if c["id"] != item["id"]] + [item]
    save(data_dir, items)
    return item


def login_options(rp_id: str) -> tuple[str, str]:
    opts = generate_authentication_options(rp_id=rp_id, user_verification=UserVerificationRequirement.REQUIRED)
    return options_to_json(opts), bytes_to_base64url(opts.challenge)


def login_verify(data_dir: Path, credential: dict, challenge: str, rp_id: str, origin: str) -> dict:
    items = load(data_dir)
    cid = credential.get("id") or credential.get("rawId")
    item = next((c for c in items if c["id"] == cid and c.get("rp_id") == rp_id), None)
    if not item:
        raise ValueError("등록되지 않은 패스키입니다")
    v = verify_authentication_response(
        credential=credential, expected_challenge=base64url_to_bytes(challenge), expected_rp_id=rp_id,
        expected_origin=origin, credential_public_key=base64url_to_bytes(item["public_key"]),
        credential_current_sign_count=item.get("sign_count", 0), require_user_verification=True)
    item["sign_count"], item["last_used"] = v.new_sign_count, time.strftime("%Y-%m-%d %H:%M")
    save(data_dir, items)
    return item


def remove(data_dir: Path, cid: str) -> bool:
    items = load(data_dir)
    rest = [c for c in items if c["id"] != cid]
    save(data_dir, rest)
    return len(rest) != len(items)
