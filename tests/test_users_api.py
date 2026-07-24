"""사용자 관리 API + 페이지 테스트 (이슈 #18 · 조각 2).

저장소 파일은 tmp_path + monkeypatch(users.USERS_FILE)로 격리한다(test_users.py 와 동일 패턴).
env 계정 자격증명은 server import 전에 결정적으로 고정한다. system_admin 권한은 env uploader
토큰(레거시 uploader → 정규화 system_admin)으로, admin/stored system_admin 은 저장소 계정으로 얻는다.
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

# 브라우저가 보내는 Sec-Fetch-* (accept text/html 과 함께 미인증 리다이렉트 판정).
BROWSER = {"accept": "text/html", "sec-fetch-mode": "navigate"}


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    """각 테스트의 사용자 저장소를 임시 파일로 격리(server._identify 도 이 전역을 읽는다)."""
    monkeypatch.setattr(users, "USERS_FILE", tmp_path / "users.json")


def _client_for(sub: str, role: str) -> TestClient:
    """주어진 sub/role 로 서명한 JWT 쿠키를 심은 TestClient."""
    c = TestClient(app)
    c.cookies.set("reports_token", server._make_token(sub, role))
    return c


def sysadmin() -> TestClient:
    """env uploader 토큰 = 정규화 system_admin(저장소 계정 불필요)."""
    return _client_for("uploader", "uploader")


def _stored_admin(email: str = "adm@x.com", pw: str = "pw") -> TestClient:
    """저장소 admin 계정을 만들고 그 계정 토큰 클라이언트를 반환."""
    users.add_user(email, pw, "admin")
    return _client_for(email, "admin")


# ── AC1: system_admin 이 4역할 등록 · 해시/비번 미노출 ──────────────────────────
def test_sysadmin_creates_all_four_roles_no_secret_leak():
    c = sysadmin()
    for i, role in enumerate(server_roles := ["system_admin", "admin", "user", "viewer"]):
        r = c.post("/api/v1/users", json={"email": f"u{i}@x.com", "password": "pw", "role": role})
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["role"] == role
        for secret in ("pw_hash", "pw_salt", "password"):
            assert secret not in body
    listed = c.get("/api/v1/users").json()
    assert len(listed) == len(server_roles)
    for u in listed:
        for secret in ("pw_hash", "pw_salt", "password"):
            assert secret not in u


# ── AC2: admin 은 user·viewer 만 등록, admin·system_admin 은 403 ────────────────
def test_admin_can_create_user_and_viewer():
    c = _stored_admin()
    for role in ("user", "viewer"):
        r = c.post("/api/v1/users", json={"email": f"{role}@x.com", "password": "pw", "role": role})
        assert r.status_code == 201, r.text


def test_admin_cannot_create_admin_or_sysadmin():
    c = _stored_admin()
    for role in ("admin", "system_admin"):
        r = c.post("/api/v1/users", json={"email": f"x_{role}@x.com", "password": "pw", "role": role})
        assert r.status_code == 403, r.text


# ── AC3: user·viewer 금지(403), 미인증(json) 401 ───────────────────────────────
def test_user_and_viewer_forbidden_on_api():
    for role in ("user", "viewer"):
        users.add_user(f"{role}2@x.com", "pw", role)
        c = _client_for(f"{role}2@x.com", role)
        assert c.get("/api/v1/users").status_code == 403
        r = c.post("/api/v1/users", json={"email": "n@x.com", "password": "pw", "role": "user"})
        assert r.status_code == 403


def test_unauthenticated_api_returns_401():
    c = TestClient(app)
    assert c.get("/api/v1/users", headers={"accept": "application/json"}).status_code == 401
    r = c.post("/api/v1/users", headers={"accept": "application/json"},
               json={"email": "n@x.com", "password": "pw", "role": "user"})
    assert r.status_code == 401


# ── AC7: 이메일 형식 오류 422, 중복 409 ────────────────────────────────────────
def test_create_bad_email_returns_422():
    r = sysadmin().post("/api/v1/users", json={"email": "not-an-email", "password": "pw", "role": "user"})
    assert r.status_code == 422


def test_create_duplicate_returns_409():
    c = sysadmin()
    c.post("/api/v1/users", json={"email": "dup@x.com", "password": "pw", "role": "user"})
    r = c.post("/api/v1/users", json={"email": "dup@x.com", "password": "pw2", "role": "user"})
    assert r.status_code == 409


def test_create_unknown_role_returns_422():
    r = sysadmin().post("/api/v1/users", json={"email": "z@x.com", "password": "pw", "role": "superuser"})
    assert r.status_code == 422


# ── AC4: PATCH active/password/role 반영 ───────────────────────────────────────
def test_patch_active_toggles_login():
    c = sysadmin()
    c.post("/api/v1/users", json={"email": "t1@x.com", "password": "pw", "role": "user"})
    assert c.patch("/api/v1/users/t1@x.com", json={"active": False}).status_code == 200
    denied = TestClient(app).post("/login", data={"username": "t1@x.com", "password": "pw", "next": "/"},
                                  follow_redirects=False)
    assert denied.status_code == 401  # 정지 → 로그인 거부
    assert c.patch("/api/v1/users/t1@x.com", json={"active": True}).status_code == 200
    ok = TestClient(app).post("/login", data={"username": "t1@x.com", "password": "pw", "next": "/"},
                              follow_redirects=False)
    assert ok.status_code == 303  # 재활성화 → 복구


def test_patch_password_swaps_credentials():
    c = sysadmin()
    c.post("/api/v1/users", json={"email": "t2@x.com", "password": "old", "role": "user"})
    assert c.patch("/api/v1/users/t2@x.com", json={"password": "new"}).status_code == 200
    lc = TestClient(app)
    assert lc.post("/login", data={"username": "t2@x.com", "password": "new", "next": "/"},
                   follow_redirects=False).status_code == 303
    assert lc.post("/login", data={"username": "t2@x.com", "password": "old", "next": "/"},
                   follow_redirects=False).status_code == 401


def test_patch_role_reflected_in_list():
    c = sysadmin()
    c.post("/api/v1/users", json={"email": "t3@x.com", "password": "pw", "role": "viewer"})
    assert c.patch("/api/v1/users/t3@x.com", json={"role": "admin"}).status_code == 200
    listed = {u["email"]: u for u in c.get("/api/v1/users").json()}
    assert listed["t3@x.com"]["role"] == "admin"


# ── AC1 확장: PATCH 응답에도 해시/비번 미노출(생성·목록뿐 아니라 수정도) ────────
def test_patch_response_never_leaks_secrets():
    c = sysadmin()
    c.post("/api/v1/users", json={"email": "t4@x.com", "password": "pw", "role": "user"})
    for payload in ({"password": "pw2"}, {"role": "viewer"}, {"active": False}):
        r = c.patch("/api/v1/users/t4@x.com", json=payload)
        assert r.status_code == 200, r.text
        body = r.json()
        for secret in ("pw_hash", "pw_salt", "password"):
            assert secret not in body


# ── AC5: admin 이 상위 계정 PATCH 403, 부재/env 가상 404, 본인 정지·강등 409, 빈 PATCH 422 ─
def test_admin_cannot_patch_admin_or_sysadmin_account():
    c_sys = sysadmin()
    c_sys.post("/api/v1/users", json={"email": "other_admin@x.com", "password": "pw", "role": "admin"})
    c_sys.post("/api/v1/users", json={"email": "other_sys@x.com", "password": "pw", "role": "system_admin"})
    c = _stored_admin()
    assert c.patch("/api/v1/users/other_admin@x.com", json={"password": "x"}).status_code == 403
    assert c.patch("/api/v1/users/other_sys@x.com", json={"active": False}).status_code == 403


def test_admin_cannot_promote_user_to_admin():
    c_sys = sysadmin()
    c_sys.post("/api/v1/users", json={"email": "plain@x.com", "password": "pw", "role": "user"})
    c = _stored_admin()
    assert c.patch("/api/v1/users/plain@x.com", json={"role": "admin"}).status_code == 403


def test_patch_nonexistent_returns_404():
    assert sysadmin().patch("/api/v1/users/ghost@x.com", json={"role": "user"}).status_code == 404


def test_patch_env_virtual_account_returns_404():
    c = sysadmin()
    assert c.patch("/api/v1/users/uploader", json={"active": False}).status_code == 404
    assert c.patch("/api/v1/users/reader", json={"role": "user"}).status_code == 404


def test_self_suspend_and_downgrade_blocked_409():
    users.add_user("root@x.com", "pw", "system_admin")
    c = _client_for("root@x.com", "system_admin")
    assert c.patch("/api/v1/users/root@x.com", json={"active": False}).status_code == 409
    assert c.patch("/api/v1/users/root@x.com", json={"role": "admin"}).status_code == 409
    # 본인 비밀번호 변경·재활성화는 허용된다.
    assert c.patch("/api/v1/users/root@x.com", json={"password": "newpw"}).status_code == 200
    assert c.patch("/api/v1/users/root@x.com", json={"active": True}).status_code == 200


def test_empty_patch_returns_422():
    c = sysadmin()
    c.post("/api/v1/users", json={"email": "e1@x.com", "password": "pw", "role": "user"})
    assert c.patch("/api/v1/users/e1@x.com", json={}).status_code == 422


# ── AC6: 페이지 렌더 · 권한 ───────────────────────────────────────────────────
def test_users_page_renders_for_sysadmin():
    r = sysadmin().get("/admin/users")
    assert r.status_code == 200
    body = r.text
    assert 'id="adduser"' in body                   # 신규 등록 폼
    assert 'table class="users"' in body            # 목록 테이블
    assert "정지" in body and "활성화" in body        # 정지/활성화 액션
    assert "역할 변경" in body and "비밀번호 재설정" in body  # 정보 수정 액션
    # system_admin 등록 폼엔 4역할 전부 노출.
    select = body.split('id="urole"')[1].split("</select>")[0]
    for role in ("system_admin", "admin", "user", "viewer"):
        assert f'value="{role}"' in select


def test_users_page_admin_form_hides_elevated_roles():
    r = _stored_admin().get("/admin/users")
    assert r.status_code == 200
    select = r.text.split('id="urole"')[1].split("</select>")[0]
    assert 'value="user"' in select and 'value="viewer"' in select
    assert 'value="admin"' not in select and 'value="system_admin"' not in select


def test_users_page_forbidden_for_user_and_viewer():
    for role in ("user", "viewer"):
        users.add_user(f"pg_{role}@x.com", "pw", role)
        c = _client_for(f"pg_{role}@x.com", role)
        r = c.get("/admin/users", headers={"accept": "text/html"}, follow_redirects=False)
        assert r.status_code == 403


def test_users_page_unauth_browser_redirects_to_login():
    c = TestClient(app)
    r = c.get("/admin/users", headers=BROWSER, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login")
