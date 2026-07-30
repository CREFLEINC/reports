"""Independent acceptance test for issue #23 token TTL compatibility."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


_TOKEN_PROBE = """
import json

from fastapi.testclient import TestClient

import server

response = TestClient(server.app).post(
    "/api/v1/auth/token",
    data={"username": "uploader", "password": "uploaderpass"},
)
body = response.json()
payload = server._decode_token(body["access_token"])
assert payload is not None
print(json.dumps({
    "status_code": response.status_code,
    "configured_ttl": server.API_TOKEN_TTL,
    "expires_in": body["expires_in"],
    "jwt_ttl": payload["exp"] - payload["iat"],
}))
"""


def _probe_token_ttl(api_token_ttl: str | None) -> dict[str, int]:
    """Run the token endpoint in a fresh process with an isolated API TTL."""
    env = os.environ.copy()
    env.update(
        {
            "REPORTS_USER": "reader",
            "REPORTS_PASS": "readerpass",
            "REPORTS_UPLOAD_USER": "uploader",
            "REPORTS_UPLOAD_PASS": "uploaderpass",
            "REPORTS_SECRET_KEY": "test-secret-deadbeef-0123456789abcdef",
        }
    )
    if api_token_ttl is None:
        env.pop("REPORTS_API_TOKEN_TTL", None)
    else:
        env["REPORTS_API_TOKEN_TTL"] = api_token_ttl
    completed = subprocess.run(
        [sys.executable, "-c", _TOKEN_PROBE],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        check=True,
        text=True,
    )
    return json.loads(completed.stdout)


@pytest.mark.parametrize(
    ("api_token_ttl", "expected_ttl"),
    [(None, 86400), ("321", 321)],
    ids=("default", "override"),
)
def test_api_token_ttl_environment_isolated(api_token_ttl, expected_ttl):
    """Each supported environment configuration controls the token response."""
    assert _probe_token_ttl(api_token_ttl) == {
        "status_code": 200,
        "configured_ttl": expected_ttl,
        "expires_in": expected_ttl,
        "jwt_ttl": expected_ttl,
    }


def test_make_token_default_uses_current_browser_ttl(monkeypatch):
    """Default calls must retain the pre-#23 runtime TOKEN_TTL lookup behavior."""
    import server

    monkeypatch.setattr(server, "TOKEN_TTL", 321)

    payload = server._decode_token(server._make_token("reader", "reader"))

    assert payload is not None
    assert payload["exp"] - payload["iat"] == 321
