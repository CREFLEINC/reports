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


@pytest.fixture(autouse=True)
def _isolate_auth_failure_state():
    with server._FAILURE_STATE_LOCK:
        server._AUTH_FAILURES.clear()
        server._SHARE_UNLOCK_FAILURES.clear()
        server._AUTH_FAILURE_OVERFLOW.clear()
        server._SHARE_UNLOCK_FAILURE_OVERFLOW.clear()
    yield
    with server._FAILURE_STATE_LOCK:
        server._AUTH_FAILURES.clear()
        server._SHARE_UNLOCK_FAILURES.clear()
        server._AUTH_FAILURE_OVERFLOW.clear()
        server._SHARE_UNLOCK_FAILURE_OVERFLOW.clear()


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


@pytest.fixture
def served_doc():
    import shutil
    d = server.UPLOADS_DOCS / "demo" / "pytest_doc_v1"
    d.mkdir(parents=True, exist_ok=True)
    (d / "index.html").write_text("<title>pytest</title>ok", encoding="utf-8")
    url = "/" + (d / "index.html").relative_to(server.BASE_DIR).as_posix()
    yield url
    shutil.rmtree(server.UPLOADS_DOCS / "demo" / "pytest_doc_v1", ignore_errors=True)


def test_basic_serves_file_regression(served_doc):
    # register_report.sh 의 `curl -u ...` 반영확인과 동일 경로(Basic 헤더 → serve)
    r = client.get(served_doc, auth=("reader", "readerpass"))
    assert r.status_code == 200


def test_serve_unauth_browser_redirects(served_doc):
    r = client.get(served_doc, headers={"accept": "text/html"}, follow_redirects=False)
    assert r.status_code == 303


# 브라우저가 보내는 Sec-Fetch-* (사이트/JS가 못 지우는 forbidden header). curl 등 자동화는 안 보냄.
BROWSER = {
    "accept": "text/html",
    "sec-fetch-site": "same-origin",
    "sec-fetch-mode": "navigate",
    "sec-fetch-dest": "document",
}


def test_browser_with_cached_basic_is_not_authenticated():
    # 구 시스템에서 캐시된 Basic 을 브라우저가 자동 전송해도 브라우저 요청에선 무시 → 미인증.
    r = client.get("/", headers=BROWSER, auth=("reader", "readerpass"), follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login")


def test_login_page_renders_for_cached_basic_browser():
    # 캐시된 Basic 때문에 /login 이 곧장 리다이렉트되면 로그아웃 불가 → 폼이 떠야 한다.
    r = client.get("/login", headers=BROWSER, auth=("reader", "readerpass"), follow_redirects=False)
    assert r.status_code == 200
    assert "로그인" in r.text


def test_logout_fully_deauths_cached_basic_browser():
    c = TestClient(app)
    c.post("/logout", headers=BROWSER, auth=("reader", "readerpass"), follow_redirects=False)
    r = c.get("/", headers=BROWSER, auth=("reader", "readerpass"), follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login")


def test_automation_basic_still_honored():
    # 자동화(curl/register_report.sh): Sec-Fetch 없음 → Basic 폴백 유지(회귀 방지).
    r = client.get("/", auth=("reader", "readerpass"))
    assert r.status_code == 200


def test_browser_jwt_cookie_still_works():
    c = TestClient(app)
    c.cookies.set("reports_token", server._make_token("reader", "reader"))
    r = c.get("/", headers=BROWSER)
    assert r.status_code == 200


# ── 토큰 발급 API + Bearer 인증 (이슈 #19) ──────────────────────────────────
def test_token_endpoint_issues_uploader_jwt():
    # AC1: 유효 uploader 자격증명 → 200, access_token/token_type/expires_in, sub·role 복원 가능.
    r = client.post("/api/v1/auth/token", data={"username": "uploader", "password": "uploaderpass"})
    assert r.status_code == 200
    body = r.json()
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] == server.TOKEN_TTL
    payload = server._decode_token(body["access_token"])
    assert payload is not None
    assert payload["sub"] == "uploader"
    assert payload["role"] == "uploader"


def test_token_endpoint_issues_reader_jwt():
    # AC1: 유효 reader 자격증명 → 200, reader 역할 토큰.
    r = client.post("/api/v1/auth/token", data={"username": "reader", "password": "readerpass"})
    assert r.status_code == 200
    payload = server._decode_token(r.json()["access_token"])
    assert payload is not None
    assert payload["sub"] == "reader"
    assert payload["role"] == "reader"


def test_token_endpoint_wrong_credentials_401():
    # AC2: 잘못된 자격증명 → 401 + JSON detail, 토큰 미발급.
    r = client.post("/api/v1/auth/token", data={"username": "reader", "password": "WRONG"})
    assert r.status_code == 401
    body = r.json()
    assert "access_token" not in body
    assert "detail" in body


# ── 자격증 실패 시도 제한 (이슈 #22) ───────────────────────────
def test_login_and_token_failures_share_ip_bucket(monkeypatch):
    now = 100.0
    monkeypatch.setattr(server.time, "monotonic", lambda: now)
    c = TestClient(app, client=("198.51.100.10", 50000))

    for path in ("/login", "/api/v1/auth/token", "/login", "/api/v1/auth/token"):
        r = c.post(path, data={"username": "reader", "password": "WRONG", "next": "/"})
        assert r.status_code == 401

    locked = c.post(
        "/login",
        data={"username": "reader", "password": "WRONG", "next": "/"},
    )
    assert locked.status_code == 429
    assert locked.headers["retry-after"] == "60"


def test_locked_auth_rejects_valid_credentials_with_remaining_seconds(monkeypatch):
    now = [200.0]
    monkeypatch.setattr(server.time, "monotonic", lambda: now[0])
    c = TestClient(app, client=("198.51.100.11", 50000))

    for _ in range(5):
        c.post("/api/v1/auth/token", data={"username": "reader", "password": "WRONG"})

    now[0] = 201.2
    locked = c.post(
        "/login",
        data={"username": "reader", "password": "readerpass", "next": "/"},
    )
    assert locked.status_code == 429
    assert locked.headers["retry-after"] == "59"


def test_auth_lock_expires_and_success_resets_failures(monkeypatch):
    now = [300.0]
    monkeypatch.setattr(server.time, "monotonic", lambda: now[0])
    c = TestClient(app, client=("198.51.100.12", 50000))

    for _ in range(4):
        failed = c.post(
            "/login",
            data={"username": "reader", "password": "WRONG", "next": "/"},
        )
        assert failed.status_code == 401
    success = c.post(
        "/api/v1/auth/token",
        data={"username": "reader", "password": "readerpass"},
    )
    assert success.status_code == 200

    for _ in range(4):
        failed = c.post(
            "/login",
            data={"username": "reader", "password": "WRONG", "next": "/"},
        )
        assert failed.status_code == 401

    locked = c.post(
        "/api/v1/auth/token",
        data={"username": "reader", "password": "WRONG"},
    )
    assert locked.status_code == 429

    now[0] = 360.0
    after_expiry = c.post(
        "/api/v1/auth/token",
        data={"username": "reader", "password": "readerpass"},
    )
    assert after_expiry.status_code == 200


def test_auth_failure_buckets_are_isolated_by_ip(monkeypatch):
    monkeypatch.setattr(server.time, "monotonic", lambda: 400.0)
    locked_client = TestClient(app, client=("198.51.100.13", 50000))
    other_client = TestClient(app, client=("198.51.100.14", 50000))

    for _ in range(5):
        locked_client.post(
            "/login",
            data={"username": "reader", "password": "WRONG", "next": "/"},
        )

    success = other_client.post(
        "/login",
        data={"username": "reader", "password": "readerpass", "next": "/"},
        follow_redirects=False,
    )
    assert success.status_code == 303


def test_success_for_one_username_does_not_reset_another_username(monkeypatch):
    monkeypatch.setattr(server.time, "monotonic", lambda: 850.0)
    c = TestClient(app, client=("198.51.100.16", 50000))

    for path in ("/login", "/api/v1/auth/token", "/login", "/api/v1/auth/token"):
        failed = c.post(
            path,
            data={"username": "uploader", "password": "WRONG", "next": "/"},
        )
        assert failed.status_code == 401

    reader_success = c.post(
        "/api/v1/auth/token",
        data={"username": "reader", "password": "readerpass"},
    )
    assert reader_success.status_code == 200

    uploader_locked = c.post(
        "/login",
        data={"username": "uploader", "password": "WRONG", "next": "/"},
    )
    assert uploader_locked.status_code == 429


def test_partial_auth_failures_expire_after_sixty_idle_seconds(monkeypatch):
    now = [900.0]
    monkeypatch.setattr(server.time, "monotonic", lambda: now[0])
    c = TestClient(app, client=("198.51.100.15", 50000))

    for _ in range(4):
        failed = c.post(
            "/login",
            data={"username": "reader", "password": "WRONG", "next": "/"},
        )
        assert failed.status_code == 401

    now[0] = 960.0
    after_expiry = c.post(
        "/api/v1/auth/token",
        data={"username": "reader", "password": "WRONG"},
    )
    assert after_expiry.status_code == 401


def test_auth_failure_bucket_fails_closed_without_evicting_active_entries(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(server.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(server, "_FAILURE_BUCKET_LIMIT", 3, raising=False)

    victim = TestClient(app, client=("198.51.100.1", 50000))
    for _ in range(5):
        victim.post(
            "/login",
            data={"username": "reader", "password": "WRONG", "next": "/"},
        )
    now[0] = 1001.0
    for suffix in (2, 3):
        attempt_client = TestClient(app, client=(f"198.51.100.{suffix}", 50000))
        assert attempt_client.post(
            "/login",
            data={"username": "reader", "password": "WRONG", "next": "/"},
        ).status_code == 401

    before_churn = dict(server._AUTH_FAILURES)
    now[0] = 1002.0
    for suffix in (4, 5, 6):
        attempt_client = TestClient(app, client=(f"198.51.100.{suffix}", 50000))
        saturated = attempt_client.post(
            "/login",
            data={"username": "reader", "password": "WRONG", "next": "/"},
        )
        assert saturated.status_code == 429
        assert saturated.headers["retry-after"] == "58"

    assert len(server._AUTH_FAILURES) == 3
    assert server._AUTH_FAILURES == before_churn
    locked = victim.post(
        "/login",
        data={"username": "reader", "password": "readerpass", "next": "/"},
    )
    assert locked.status_code == 429


def test_saturated_auth_bucket_allows_valid_login_and_token(monkeypatch):
    monkeypatch.setattr(server.time, "monotonic", lambda: 1100.0)
    monkeypatch.setattr(server, "_FAILURE_BUCKET_LIMIT", 3, raising=False)

    for suffix in (1, 2, 3):
        attempt_client = TestClient(app, client=(f"198.51.100.{suffix}", 50000))
        failed = attempt_client.post(
            "/login",
            data={"username": "reader", "password": "WRONG", "next": "/"},
        )
        assert failed.status_code == 401

    before_success = dict(server._AUTH_FAILURES)
    valid_login = TestClient(app, client=("198.51.100.7", 50000)).post(
        "/login",
        data={"username": "reader", "password": "readerpass", "next": "/"},
        follow_redirects=False,
    )
    assert valid_login.status_code == 303
    assert server._AUTH_FAILURES == before_success
    valid_token = TestClient(app, client=("198.51.100.8", 50000)).post(
        "/api/v1/auth/token",
        data={"username": "reader", "password": "readerpass"},
    )
    assert valid_token.status_code == 200
    assert server._AUTH_FAILURES == before_success


def test_saturated_auth_overflow_limits_ip_after_interleaved_success(
    monkeypatch,
):
    monkeypatch.setattr(server.time, "monotonic", lambda: 1200.0)
    monkeypatch.setattr(server, "_FAILURE_BUCKET_LIMIT", 2, raising=False)

    for suffix in (1, 2):
        attempt_client = TestClient(app, client=(f"198.51.100.{suffix}", 50000))
        failed = attempt_client.post(
            "/login",
            data={"username": "reader", "password": "WRONG", "next": "/"},
        )
        assert failed.status_code == 401

    before_overflow = dict(server._AUTH_FAILURES)
    verified_credentials = []
    verify_credentials = server._role_for_credentials

    def count_verification(username, password):
        verified_credentials.append((username, password))
        return verify_credentials(username, password)

    monkeypatch.setattr(server, "_role_for_credentials", count_verification)
    overflow_client = TestClient(app, client=("198.51.100.50", 50000))
    for attempt in range(4):
        path = "/login" if attempt % 2 == 0 else "/api/v1/auth/token"
        response = overflow_client.post(
            path,
            data={"username": f"attacker-{attempt}", "password": "WRONG", "next": "/"},
        )
        assert response.status_code == 429

    valid = overflow_client.post(
        "/api/v1/auth/token",
        data={"username": "reader", "password": "readerpass"},
    )
    assert valid.status_code == 200
    fifth_failure = overflow_client.post(
        "/login",
        data={"username": "attacker-4", "password": "WRONG", "next": "/"},
    )
    assert fifth_failure.status_code == 429
    assert [password for _username, password in verified_credentials].count("WRONG") == 5

    before_locked_attempt = list(verified_credentials)
    locked = overflow_client.post(
        "/api/v1/auth/token",
        data={"username": "attacker-5", "password": "WRONG"},
    )
    assert locked.status_code == 429
    assert locked.headers["retry-after"] == "60"
    assert verified_credentials == before_locked_attempt
    assert server._AUTH_FAILURES == before_overflow


def test_saturated_auth_overflow_reuses_bounded_capacity_in_expiry_order(monkeypatch):
    now = [1250.0]
    monkeypatch.setattr(server.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(server, "_FAILURE_BUCKET_LIMIT", 1, raising=False)
    monkeypatch.setattr(server, "_FAILURE_OVERFLOW_BUCKET_LIMIT", 2, raising=False)

    primary_client = TestClient(app, client=("198.51.100.60", 50000))
    assert primary_client.post(
        "/login",
        data={"username": "reader", "password": "WRONG", "next": "/"},
    ).status_code == 401

    for suffix in (61, 62):
        overflow_client = TestClient(app, client=(f"198.51.100.{suffix}", 50000))
        assert overflow_client.post(
            "/login",
            data={"username": f"attacker-{suffix}", "password": "WRONG", "next": "/"},
        ).status_code == 429
        now[0] += 1

    assert len(server._AUTH_FAILURE_OVERFLOW) == 2
    newcomer = TestClient(app, client=("198.51.100.63", 50000))
    valid = newcomer.post(
        "/login",
        data={"username": "reader", "password": "readerpass", "next": "/"},
        follow_redirects=False,
    )
    assert valid.status_code == 303
    assert len(server._AUTH_FAILURE_OVERFLOW) == 2
    failed = newcomer.post(
        "/login",
        data={"username": "attacker-63", "password": "WRONG", "next": "/"},
    )
    assert failed.status_code == 429
    assert len(server._AUTH_FAILURE_OVERFLOW) == 2
    assert "198.51.100.61" not in server._AUTH_FAILURE_OVERFLOW
    assert "198.51.100.62" in server._AUTH_FAILURE_OVERFLOW
    assert "198.51.100.63" in server._AUTH_FAILURE_OVERFLOW

    now[0] = 1254.0
    assert primary_client.post(
        "/login",
        data={"username": "reader", "password": "WRONG", "next": "/"},
    ).status_code == 401
    now[0] = 1311.0
    retried = TestClient(app, client=("198.51.100.61", 50000)).post(
        "/login",
        data={"username": "new-attacker", "password": "WRONG", "next": "/"},
    )
    assert retried.status_code == 429
    assert "198.51.100.62" not in server._AUTH_FAILURE_OVERFLOW
    assert "198.51.100.61" in server._AUTH_FAILURE_OVERFLOW


def test_auth_failure_bucket_refreshes_order_before_pruning(monkeypatch):
    now = [1300.0]
    monkeypatch.setattr(server.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(server, "_FAILURE_BUCKET_LIMIT", 2, raising=False)
    first = TestClient(app, client=("198.51.100.21", 50000))
    second = TestClient(app, client=("198.51.100.22", 50000))
    newcomer = TestClient(app, client=("198.51.100.23", 50000))

    assert first.post(
        "/login",
        data={"username": "reader", "password": "WRONG", "next": "/"},
    ).status_code == 401
    now[0] = 1301.0
    assert second.post(
        "/login",
        data={"username": "reader", "password": "WRONG", "next": "/"},
    ).status_code == 401
    now[0] = 1302.0
    assert first.post(
        "/login",
        data={"username": "reader", "password": "WRONG", "next": "/"},
    ).status_code == 401

    now[0] = 1303.0
    saturated = newcomer.post(
        "/login",
        data={"username": "reader", "password": "WRONG", "next": "/"},
    )
    assert saturated.status_code == 429
    assert saturated.headers["retry-after"] == "58"

    now[0] = 1361.0
    accepted = newcomer.post(
        "/login",
        data={"username": "reader", "password": "WRONG", "next": "/"},
    )
    assert accepted.status_code == 401
    assert ("198.51.100.21", "reader") in server._AUTH_FAILURES
    assert ("198.51.100.22", "reader") not in server._AUTH_FAILURES


def test_bearer_token_grants_read():
    # AC1/AC6: 발급 토큰을 Authorization: Bearer 로 보내면(쿠키·Basic 없이) 읽기 인증 통과.
    tok = server._make_token("reader", "reader")
    r = client.get("/", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200


def test_bearer_forged_token_rejected():
    # AC5: 위조 Bearer 토큰 → 미인증(401).
    r = client.get("/", headers={"Authorization": "Bearer not.a.jwt", "accept": "application/json"},
                   follow_redirects=False)
    assert r.status_code == 401


def test_bearer_expired_token_rejected():
    # AC5: 만료 Bearer 토큰 → 미인증(401).
    now = int(time.time())
    tok = jwt.encode({"sub": "reader", "role": "reader", "iat": now - 100, "exp": now - 10},
                     server.SECRET_KEY, algorithm="HS256")
    r = client.get("/", headers={"Authorization": f"Bearer {tok}", "accept": "application/json"},
                   follow_redirects=False)
    assert r.status_code == 401


# ── HTTP 스펙 정합성: WWW-Authenticate · no-store · 스킴 대소문자 (이슈 #21) ──────
CHALLENGE = 'Bearer, Basic realm="reports"'


def test_www_authenticate_on_index_401():
    # AC1: 미인증 API(json) 401(verify_identity)에 Bearer/Basic 챌린지 헤더.
    r = client.get("/", headers={"accept": "application/json"}, follow_redirects=False)
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == CHALLENGE


def test_www_authenticate_on_upload_401():
    # AC1: 미인증 /upload API 401(require_can_upload)에 챌린지 헤더.
    r = client.get("/upload", headers={"accept": "application/json"}, follow_redirects=False)
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == CHALLENGE


def test_www_authenticate_on_users_api_401():
    # AC1: 미인증 /api/v1/users 401(require_admin)에 챌린지 헤더.
    r = client.get("/api/v1/users", headers={"accept": "application/json"}, follow_redirects=False)
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == CHALLENGE


def test_www_authenticate_on_forged_bearer_401():
    # AC1: 위조 Bearer 토큰 미인증 401 에도 챌린지 헤더.
    r = client.get("/", headers={"Authorization": "Bearer not.a.jwt", "accept": "application/json"},
                   follow_redirects=False)
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == CHALLENGE


def test_www_authenticate_on_suspended_account_token_401(tmp_path, monkeypatch):
    # AC1: 정지(active=False) 계정의 기발급 Bearer 토큰 → 401 + 챌린지 헤더.
    import users
    monkeypatch.setattr(users, "USERS_FILE", tmp_path / "users.json")
    users.add_user("susp@x.com", "pw", "user")
    users.set_active("susp@x.com", False)
    tok = server._make_token("susp@x.com", "user")
    r = client.get("/", headers={"Authorization": f"Bearer {tok}", "accept": "application/json"},
                   follow_redirects=False)
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == CHALLENGE


def test_no_www_authenticate_on_403():
    # AC1: 권한 부족 403 은 챌린지가 아니므로 WWW-Authenticate 없음(reader → /upload).
    c = TestClient(app)
    c.cookies.set("reports_token", server._make_token("reader", "reader"))
    r = c.get("/upload", headers={"accept": "application/json"}, follow_redirects=False)
    assert r.status_code == 403
    assert "www-authenticate" not in r.headers


def test_token_response_has_no_store():
    # AC2: 토큰 발급 200 응답에 Cache-Control: no-store(RFC 6749 §5.1).
    r = client.post("/api/v1/auth/token", data={"username": "reader", "password": "readerpass"})
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"


def test_bearer_scheme_case_insensitive():
    # AC3: 소문자/대문자 'bearer' 스킴도 인증 성공(RFC 7235, 토큰 값 불변).
    tok = server._make_token("reader", "reader")
    for scheme in ("bearer", "BEARER", "BeArEr"):
        r = client.get("/", headers={"Authorization": f"{scheme} {tok}"})
        assert r.status_code == 200, scheme


def test_basic_scheme_case_insensitive():
    # AC3: 소문자 'basic' 스킴도 자동화 폴백으로 인증 성공(Sec-Fetch 없음).
    import base64
    cred = base64.b64encode(b"reader:readerpass").decode()
    r = client.get("/", headers={"Authorization": f"basic {cred}"})
    assert r.status_code == 200
