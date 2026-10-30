"""패스키 — 주소 검증과 저장 (브라우저 흐름은 Playwright 가상 인증기로 따로 확인)."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from web import passkeys  # noqa: E402


def test_origin_rules():
    assert passkeys.rp_and_origin("stock.example.com", "https://stock.example.com") == \
        ("stock.example.com", "https://stock.example.com")
    for host, origin in [("stock.example.com", "http://stock.example.com"),     # https 아님
                         ("stock.example.com", "https://evil.com"),             # 다른 도메인
                         ("192.168.0.10:8080", "http://192.168.0.10:8080")]:    # IP + http
        try:
            passkeys.rp_and_origin(host, origin)
        except ValueError:
            continue
        raise AssertionError((host, origin))
    assert passkeys.rp_and_origin("localhost:8080", "http://localhost:8080")[0] == "localhost"
    os.environ["WEB_PUBLIC_HOST"] = "stock.example.com"
    try:
        assert passkeys.rp_and_origin("127.0.0.1:8080", "https://stock.example.com")[0] == "stock.example.com"
    finally:
        os.environ.pop("WEB_PUBLIC_HOST")


def test_store_and_remove():
    d = Path(tempfile.mkdtemp())
    passkeys.save(d, [{"id": "a", "public_key": "x", "rp_id": "h"}, {"id": "b", "public_key": "y", "rp_id": "h"}])
    assert oct((d / "passkeys.json").stat().st_mode)[-3:] == "600"
    assert passkeys.remove(d, "a") and [c["id"] for c in passkeys.load(d)] == ["b"]
    assert not passkeys.remove(d, "zz")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
