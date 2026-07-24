"""파일 기반 사용자 저장소 + 인증 통합 테스트 (이슈 #18 · 조각 1).

저장소 파일은 tmp_path + monkeypatch(users.USERS_FILE)로 격리한다 — 실제 uploads/users.json
을 건드리지 않는다. env 계정 자격증명은 server import 전에 결정적으로 고정한다(test_auth.py 와
동일 값). 통합 테스트는 TestClient 로 /login·/api/v1/auth/token·GET / 를 검증한다.
"""
import os

# server import 전에 결정적 자격증명/키를 고정(다른 테스트 파일이 먼저 설정했으면 유지).
os.environ.setdefault("REPORTS_USER", "reader")
os.environ.setdefault("REPORTS_PASS", "readerpass")
os.environ.setdefault("REPORTS_UPLOAD_USER", "uploader")
os.environ.setdefault("REPORTS_UPLOAD_PASS", "uploaderpass")
os.environ.setdefault("REPORTS_SECRET_KEY", "test-secret-deadbeef-0123456789abcdef")

import pytest
from fastapi.testclient import TestClient

import server
import users
from server import app


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    """각 테스트의 사용자 저장소를 임시 파일로 격리(server._identify 도 이 전역을 읽는다)."""
    monkeypatch.setattr(users, "USERS_FILE", tmp_path / "users.json")


# ── users 모듈 단위: 등록·검증·정규화 ────────────────────────────────────────
def test_add_user_hashes_and_lists(tmp_path):
    pub = users.add_user("a@x.com", "pw1", "user")
    assert pub["email"] == "a@x.com"
    assert pub["role"] == "user"
    assert pub["active"] is True
    assert "pw_hash" not in pub and "pw_salt" not in pub  # 해시·비번 미노출

    # 파일에 평문 비밀번호가 저장되지 않는다(pbkdf2 해시만).
    raw = users.USERS_FILE.read_text(encoding="utf-8")
    assert "pw1" not in raw

    listed = users.list_users()
    assert [u["email"] for u in listed] == ["a@x.com"]
    assert "pw_hash" not in listed[0]

    # 내부 레코드에는 해시가 있고 자격증명 검증이 통과한다.
    rec = users.get_user("a@x.com")
    assert rec["pw_hash"] and rec["pw_salt"]
    assert users.verify_credentials("a@x.com", "pw1") == "user"
    assert users.verify_credentials("a@x.com", "wrong") is None


def test_add_user_rejects_bad_email():
    with pytest.raises(ValueError):
        users.add_user("not-an-email", "pw1", "user")


def test_add_user_rejects_duplicate():
    users.add_user("dup@x.com", "pw1", "user")
    with pytest.raises(ValueError):
        users.add_user("dup@x.com", "pw2", "admin")


def test_add_user_rejects_reserved_env_email(monkeypatch):
    monkeypatch.setenv("REPORTS_UPLOAD_USER", "boss@corp.com")
    with pytest.raises(ValueError):
        users.add_user("boss@corp.com", "pw1", "admin")
    # 예약되지 않은 email 은 정상 등록.
    assert users.add_user("someone@corp.com", "pw1", "user")["email"] == "someone@corp.com"


def test_add_user_rejects_unknown_role():
    with pytest.raises(ValueError):
        users.add_user("b@x.com", "pw1", "superuser")


def test_add_user_rejects_empty_password():
    with pytest.raises(ValueError):
        users.add_user("c@x.com", "", "user")


def test_email_normalized_to_lowercase():
    users.add_user("Mixed@Case.COM", "pw1", "user")
    assert users.get_user("mixed@case.com") is not None
    assert users.verify_credentials("MIXED@CASE.com", "pw1") == "user"


def test_set_active_blocks_and_restores_credentials():
    users.add_user("s@x.com", "pw1", "user")
    users.set_active("s@x.com", False)
    assert users.verify_credentials("s@x.com", "pw1") is None  # 정지 → 인증 거부
    users.set_active("s@x.com", True)
    assert users.verify_credentials("s@x.com", "pw1") == "user"  # 재활성화 복구


def test_update_user_password_and_role():
    users.add_user("u@x.com", "old", "viewer")
    users.update_user("u@x.com", password="new", role="admin")
    assert users.verify_credentials("u@x.com", "old") is None
    assert users.verify_credentials("u@x.com", "new") == "admin"


def test_update_user_missing_raises():
    with pytest.raises(ValueError):
        users.update_user("ghost@x.com", role="user")


def test_normalize_role_legacy_and_identity():
    assert users.normalize_role("uploader") == "system_admin"
    assert users.normalize_role("reader") == "viewer"
    for role in ("system_admin", "admin", "user", "viewer"):
        assert users.normalize_role(role) == role
    with pytest.raises(ValueError):
        users.normalize_role("root")


def test_role_at_least_hierarchy():
    assert users.role_at_least("system_admin", "admin") is True
    assert users.role_at_least("admin", "system_admin") is False
    assert users.role_at_least("uploader", "admin") is True  # 레거시도 정규화 후 비교
    assert users.role_at_least("viewer", "viewer") is True
    assert users.role_at_least("user", "admin") is False


# ── 통합: 저장소 사용자 로그인·토큰·정지 ──────────────────────────────────────
def test_registered_user_can_login_and_browse():
    users.add_user("login@x.com", "pw1", "user")
    c = TestClient(app)
    r = c.post("/login", data={"username": "login@x.com", "password": "pw1", "next": "/"},
               follow_redirects=False)
    assert r.status_code == 303
    assert "reports_token=" in r.headers.get("set-cookie", "")
    r2 = c.get("/", headers={"accept": "text/html"})
    assert r2.status_code == 200


def test_registered_user_token_endpoint_carries_role():
    users.add_user("api@x.com", "pw1", "user")
    r = TestClient(app).post("/api/v1/auth/token",
                             data={"username": "api@x.com", "password": "pw1"})
    assert r.status_code == 200
    payload = server._decode_token(r.json()["access_token"])
    assert payload["sub"] == "api@x.com"
    assert payload["role"] == "user"  # 저장소 사용자 토큰은 4역할 그대로


def test_inactive_user_login_rejected_and_restored():
    users.add_user("z@x.com", "pw1", "user")
    users.set_active("z@x.com", False)
    c = TestClient(app)
    r = c.post("/login", data={"username": "z@x.com", "password": "pw1", "next": "/"},
               follow_redirects=False)
    assert r.status_code == 401
    users.set_active("z@x.com", True)
    r2 = c.post("/login", data={"username": "z@x.com", "password": "pw1", "next": "/"},
                follow_redirects=False)
    assert r2.status_code == 303


def test_preissued_token_rejected_when_suspended():
    users.add_user("t@x.com", "pw1", "user")
    c = TestClient(app)
    c.cookies.set("reports_token", server._make_token("t@x.com", "user"))  # 정지 이전 발급 토큰
    assert c.get("/", headers={"accept": "application/json"}).status_code == 200
    users.set_active("t@x.com", False)
    assert c.get("/", headers={"accept": "application/json"}).status_code == 401
    users.set_active("t@x.com", True)
    assert c.get("/", headers={"accept": "application/json"}).status_code == 200


def test_env_account_token_keeps_legacy_wire_role():
    # 와이어 포맷 유지: env 계정 토큰 role 클레임은 uploader/reader 그대로(정규화 전).
    up = TestClient(app).post("/api/v1/auth/token",
                              data={"username": "uploader", "password": "uploaderpass"})
    assert server._decode_token(up.json()["access_token"])["role"] == "uploader"
    rd = TestClient(app).post("/api/v1/auth/token",
                              data={"username": "reader", "password": "readerpass"})
    assert server._decode_token(rd.json()["access_token"])["role"] == "reader"
