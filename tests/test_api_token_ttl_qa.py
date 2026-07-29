"""Independent acceptance test for issue #23 token TTL compatibility."""

import os

os.environ["REPORTS_USER"] = "reader"
os.environ["REPORTS_PASS"] = "readerpass"
os.environ["REPORTS_UPLOAD_USER"] = "uploader"
os.environ["REPORTS_UPLOAD_PASS"] = "uploaderpass"
os.environ["REPORTS_SECRET_KEY"] = "test-secret-deadbeef-0123456789abcdef"

import server


def test_make_token_default_uses_current_browser_ttl(monkeypatch):
    """Default calls must retain the pre-#23 runtime TOKEN_TTL lookup behavior."""
    monkeypatch.setattr(server, "TOKEN_TTL", 321)

    payload = server._decode_token(server._make_token("reader", "reader"))

    assert payload is not None
    assert payload["exp"] - payload["iat"] == 321
