import os
import time

# server import 전에 결정적 자격증명/키를 강제 설정한다.
os.environ["REPORTS_USER"] = "reader"
os.environ["REPORTS_PASS"] = "readerpass"
os.environ["REPORTS_UPLOAD_USER"] = "uploader"
os.environ["REPORTS_UPLOAD_PASS"] = "uploaderpass"
os.environ["REPORTS_SECRET_KEY"] = "test-secret-deadbeef-0123456789abcdef"

import jwt
import pytest
from fastapi.testclient import TestClient

import server
from server import app

client = TestClient(app)


def test_healthz_no_auth():
    assert client.get("/healthz").status_code == 200


def test_token_roundtrip():
    tok = server._make_token("alice", "reader")
    payload = server._decode_token(tok)
    assert payload is not None
    assert payload["sub"] == "alice"
    assert payload["role"] == "reader"


def test_decode_rejects_tampered():
    assert server._decode_token("not.a.jwt") is None


def test_decode_rejects_expired():
    now = int(time.time())
    tok = jwt.encode({"sub": "x", "role": "reader", "iat": now - 100, "exp": now - 10},
                     server.SECRET_KEY, algorithm="HS256")
    assert server._decode_token(tok) is None


def test_role_for_credentials():
    assert server._role_for_credentials("uploader", "uploaderpass") == "uploader"
    assert server._role_for_credentials("reader", "readerpass") == "reader"
    assert server._role_for_credentials("reader", "WRONG") is None
    assert server._role_for_credentials("nobody", "x") is None


def test_browser_unauth_redirects_to_login():
    r = client.get("/", headers={"accept": "text/html"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login")


def test_api_unauth_returns_401():
    r = client.get("/", headers={"accept": "application/json"}, follow_redirects=False)
    assert r.status_code == 401


def test_basic_reader_serves_index():
    r = client.get("/", auth=("reader", "readerpass"))
    assert r.status_code == 200


def test_jwt_cookie_grants_access():
    c = TestClient(app)
    c.cookies.set("reports_token", server._make_token("reader", "reader"))
    r = c.get("/", headers={"accept": "text/html"})
    assert r.status_code == 200


def test_tampered_cookie_redirects():
    c = TestClient(app)
    c.cookies.set("reports_token", "not.a.valid.jwt")
    r = c.get("/", headers={"accept": "text/html"}, follow_redirects=False)
    assert r.status_code == 303


def test_reader_cannot_access_upload():
    c = TestClient(app)
    c.cookies.set("reports_token", server._make_token("reader", "reader"))
    r = c.get("/upload", headers={"accept": "text/html"}, follow_redirects=False)
    assert r.status_code == 403


def test_uploader_can_access_upload():
    c = TestClient(app)
    c.cookies.set("reports_token", server._make_token("uploader", "uploader"))
    r = c.get("/upload")
    assert r.status_code == 200


def test_login_sets_cookie_and_grants_access():
    c = TestClient(app)
    r = c.post("/login", data={"username": "reader", "password": "readerpass", "next": "/"},
               follow_redirects=False)
    assert r.status_code == 303
    assert "reports_token=" in r.headers.get("set-cookie", "")
    r2 = c.get("/", headers={"accept": "text/html"})
    assert r2.status_code == 200


def test_login_wrong_credentials_rejected():
    c = TestClient(app)
    r = c.post("/login", data={"username": "reader", "password": "WRONG", "next": "/"},
               follow_redirects=False)
    assert r.status_code == 401
    assert "reports_token=" not in r.headers.get("set-cookie", "")


def test_login_open_redirect_blocked():
    c = TestClient(app)
    r = c.post("/login", data={"username": "reader", "password": "readerpass",
                               "next": "//evil.example.com"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/"


def test_logout_clears_cookie():
    c = TestClient(app)
    c.cookies.set("reports_token", server._make_token("reader", "reader"))
    r = c.post("/logout", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login")
    sc = r.headers.get("set-cookie", "").lower()
    assert "reports_token=" in sc and ("max-age=0" in sc or "expires=" in sc)


def test_login_page_renders():
    r = client.get("/login")
    assert r.status_code == 200
    assert "로그인" in r.text


def test_index_shows_user_and_logout():
    c = TestClient(app)
    c.cookies.set("reports_token", server._make_token("reader", "reader"))
    r = c.get("/", headers={"accept": "text/html"})
    assert r.status_code == 200
    assert "로그아웃" in r.text
    assert 'action="/logout"' in r.text
    assert "reader" in r.text
