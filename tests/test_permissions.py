"""권한 매트릭스 · 업로드 소유권 · index 관리 메뉴 (이슈 #18 · 조각 3).

계획서 권한 매트릭스가 기존 라우트에 강제되는지, 업로드 소유권(owners.json)이 overwrite 를
소유자·system_admin 으로 제한하는지, index 헤더가 역할별로 관리 메뉴/업로드/공개 버튼을
노출하는지 검증한다.

저장소·업로드 볼륨은 tmp_path + monkeypatch 로 격리한다(test_users_api.py·test_uploads.py
패턴). env 계정 자격증명은 server import 전에 결정적으로 고정한다. system_admin 은 env uploader
토큰(레거시 uploader → 정규화 system_admin)으로, admin/user/viewer 는 해당 역할 JWT 로 얻는다.
"""
import json
import os

# server import 전에 결정적 자격증명/키를 고정(다른 테스트 파일이 먼저 설정했으면 유지).
os.environ.setdefault("REPORTS_USER", "reader")
os.environ.setdefault("REPORTS_PASS", "readerpass")
os.environ.setdefault("REPORTS_UPLOAD_USER", "uploader")
os.environ.setdefault("REPORTS_UPLOAD_PASS", "uploaderpass")
os.environ.setdefault("REPORTS_SECRET_KEY", "test-secret-deadbeef-0123456789abcdef")

import pytest
from fastapi.testclient import TestClient

import doctypes
import server
import shares
import uploads_handler as uh
import users
from server import app

HTML = b"<!doctype html><title>t</title><p>ok</p>"
# 브라우저가 보내는 Sec-Fetch-*(accept text/html 과 함께 미인증 리다이렉트 판정).
BROWSER = {"accept": "text/html", "sec-fetch-mode": "navigate"}
# 게시는 실제 uploads/docs 로(핸들러가 BASE_DIR 기준 상대경로를 계산하므로 tmp 로 못 옮긴다).
# 전용 유형 슬러그에 모아 teardown 에서 통째로 제거한다(test_uploads.py 정리 패턴).
DOC_TYPE = "permtest"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """사용자·소유자·유형·공개 저장소는 tmp 로 격리하고, 게시물·렌더 큐는 teardown 에서 정리한다."""
    monkeypatch.setattr(users, "USERS_FILE", tmp_path / "users.json")
    monkeypatch.setattr(uh, "OWNERS_FILE", tmp_path / "owners.json")
    monkeypatch.setattr(doctypes, "TYPES_FILE", tmp_path / "types.json")
    monkeypatch.setattr(shares, "SHARES_FILE", tmp_path / "shares.json")
    before = set(uh.QUEUE_DIR.glob("*.json")) if uh.QUEUE_DIR.is_dir() else set()
    yield
    import shutil
    shutil.rmtree(server.UPLOADS_DOCS / DOC_TYPE, ignore_errors=True)
    if uh.QUEUE_DIR.is_dir():
        for job in set(uh.QUEUE_DIR.glob("*.json")) - before:
            job.unlink(missing_ok=True)


def _client_for(sub: str, role: str) -> TestClient:
    """주어진 sub/role 로 서명한 JWT 쿠키를 심은 TestClient(저장소 계정 불필요)."""
    c = TestClient(app)
    c.cookies.set("reports_token", server._make_token(sub, role))
    return c


def sysadmin() -> TestClient:
    """env uploader 토큰 = 정규화 system_admin(레거시 uploader 토큰 포함 검증도 겸함)."""
    return _client_for("uploader", "uploader")


def _upload(c: TestClient, name: str, version: str = "1", *, overwrite: bool = False,
            doc_type: str = DOC_TYPE, path: str = "/upload"):
    data = {"doc_type": doc_type, "name": name, "version": version}
    if overwrite:
        data["overwrite"] = "1"
    return c.post(path, data=data, files={"file": ("r.html", HTML, "text/html")})


# ── AC1: 업로드는 user 이상만(viewer 차단) — POST /upload · /api/v1/documents · GET 폼 ──
def test_upload_post_role_gating():
    assert _upload(_client_for("v@x.com", "viewer"), "g_viewer").status_code == 403
    assert _upload(_client_for("u@x.com", "user"), "g_user").status_code == 201
    assert _upload(_client_for("a@x.com", "admin"), "g_admin").status_code == 201
    assert _upload(sysadmin(), "g_sys").status_code == 201


def test_api_documents_role_gating():
    assert _upload(_client_for("v@x.com", "viewer"), "d_viewer",
                   path="/api/v1/documents").status_code == 403
    assert _upload(_client_for("u@x.com", "user"), "d_user",
                   path="/api/v1/documents").status_code == 201
    assert _upload(_client_for("a@x.com", "admin"), "d_admin",
                   path="/api/v1/documents").status_code == 201
    assert _upload(sysadmin(), "d_sys", path="/api/v1/documents").status_code == 201


def test_upload_form_role_gating():
    r = _client_for("v@x.com", "viewer").get("/upload", headers=BROWSER, follow_redirects=False)
    assert r.status_code == 403
    for role in ("user", "admin"):
        assert _client_for(f"{role}@x.com", role).get("/upload").status_code == 200
    assert sysadmin().get("/upload").status_code == 200


# ── AC2: 소유권 기록 + overwrite 소유 검사 ────────────────────────────────────
def test_ownership_recorded_on_new_upload():
    assert _upload(_client_for("a@x.com", "user"), "own").status_code == 201
    owners = json.loads(uh.OWNERS_FILE.read_text(encoding="utf-8"))
    assert owners[f"docs/{DOC_TYPE}/own_v1"]["owner"] == "a@x.com"


def test_overwrite_restricted_to_owner_and_sysadmin():
    owner = _client_for("a@x.com", "user")
    assert _upload(owner, "own").status_code == 201
    # 다른 user B → 403, 비소유 admin → 403.
    assert _upload(_client_for("b@x.com", "user"), "own", overwrite=True).status_code == 403
    assert _upload(_client_for("adm@x.com", "admin"), "own", overwrite=True).status_code == 403
    # 소유자 본인 → 201, system_admin → 201.
    assert _upload(owner, "own", overwrite=True).status_code == 201
    assert _upload(sysadmin(), "own", overwrite=True).status_code == 201


def test_admin_can_overwrite_own_upload():
    # 매트릭스: admin 의 overwrite 는 "본인 소유만" — admin 이 자신이 올린 문서를 덮어쓰는
    # 케이스는 기존 테스트에 없었다(비소유 admin 403만 확인됨). 소유자 본인이면 admin 도 201.
    owner = _client_for("adm2@x.com", "admin")
    assert _upload(owner, "admin_own").status_code == 201
    assert _upload(owner, "admin_own", overwrite=True).status_code == 201


def test_sysadmin_overwrite_preserves_original_owner():
    # system_admin 이 대신 덮어써도 원 소유자를 빼앗지 않는다(소유권 유지).
    assert _upload(_client_for("a@x.com", "user"), "keep").status_code == 201
    assert _upload(sysadmin(), "keep", overwrite=True).status_code == 201
    owners = json.loads(uh.OWNERS_FILE.read_text(encoding="utf-8"))
    assert owners[f"docs/{DOC_TYPE}/keep_v1"]["owner"] == "a@x.com"


# ── AC3: owners 기록 없는 도입 전 게시분 overwrite → system_admin 만 ───────────
def _seed_legacy_doc(name: str = "legacy", version: str = "1"):
    """owners.json 기록 없이 게시 디렉터리만 만든다(도입 전 게시분 모사)."""
    d = server.UPLOADS_DOCS / DOC_TYPE / f"{name}_v{version}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "index.html").write_bytes(HTML)


def test_legacy_doc_overwrite_sysadmin_only():
    _seed_legacy_doc()
    assert not uh.OWNERS_FILE.exists()  # 기록 없음 확인
    assert _upload(_client_for("u@x.com", "user"), "legacy", overwrite=True).status_code == 403
    assert _upload(_client_for("a@x.com", "admin"), "legacy", overwrite=True).status_code == 403
    assert _upload(sysadmin(), "legacy", overwrite=True).status_code == 201
    # system_admin 이 덮어쓴 뒤 소유권이 확립된다.
    owners = json.loads(uh.OWNERS_FILE.read_text(encoding="utf-8"))
    assert owners[f"docs/{DOC_TYPE}/legacy_v1"]["owner"] == "uploader"


# ── AC4: 유형·공개 관리는 system_admin 전용(admin·user·viewer 403) ─────────────
NON_SYSADMIN = (("admin", "admin"), ("user", "user"), ("viewer", "viewer"))


def _type_body():
    return {"slug": "permtype", "label": "권한테스트"}


def _share_body():
    return {"doc_rel": "uploads/docs/nope_v1/index.html", "expiry_date": "2099-01-01"}


def test_types_routes_forbidden_for_non_sysadmin():
    for sub, role in NON_SYSADMIN:
        c = _client_for(f"{sub}@x.com", role)
        assert c.get("/types", headers=BROWSER, follow_redirects=False).status_code == 403
        assert c.get("/api/types").status_code == 403
        assert c.post("/api/types", json=_type_body()).status_code == 403
        assert c.patch("/api/types/permtype", json={"label": "x"}).status_code == 403
        assert c.delete("/api/types/permtype").status_code == 403


def test_share_routes_forbidden_for_non_sysadmin():
    for sub, role in NON_SYSADMIN:
        c = _client_for(f"{sub}@x.com", role)
        assert c.post("/api/share", json=_share_body()).status_code == 403
        assert c.get("/api/share", params={"doc": "x"}).status_code == 403
        assert c.delete("/api/share/sometoken").status_code == 403


def test_types_and_share_allowed_for_sysadmin():
    c = sysadmin()
    # 유형: 목록·생성·이름변경·삭제 모두 인증 통과(403/401 아님).
    assert c.get("/types").status_code == 200
    assert c.get("/api/types").status_code == 200
    assert c.post("/api/types", json=_type_body()).status_code == 201
    assert c.patch("/api/types/permtype", json={"label": "새이름"}).status_code == 200
    assert c.delete("/api/types/permtype").status_code == 200
    # 공개: 게이트 통과 확인(존재하지 않는 문서/토큰이라 404·204지만 403/401 은 아님).
    assert c.get("/api/share", params={"doc": "x"}).status_code == 200
    assert c.post("/api/share", json=_share_body()).status_code == 404
    assert c.delete("/api/share/sometoken").status_code == 204


def test_legacy_uploader_token_retains_sysadmin_powers():
    # 레거시 uploader 토큰(정규화 system_admin)은 유형 관리·업로드 전 능력을 유지한다.
    c = sysadmin()
    assert c.get("/api/types").status_code == 200
    assert _upload(c, "legacy_power").status_code == 201


# ── AC5: index 헤더 — 관리 메뉴·업로드·공개 버튼 역할별 노출 ───────────────────
def _index_html(sub: str, role: str) -> str:
    r = _client_for(sub, role).get("/", headers=BROWSER)
    assert r.status_code == 200
    return r.text


def test_index_admin_menu_visible_for_admin_and_up():
    assert '/admin/users' in _index_html("s@x.com", "system_admin")
    assert '/admin/users' in _index_html("a@x.com", "admin")
    assert '/admin/users' not in _index_html("u@x.com", "user")
    assert '/admin/users' not in _index_html("v@x.com", "viewer")


def test_index_upload_button_hidden_for_viewer():
    assert '+ 업로드' not in _index_html("v@x.com", "viewer")
    for role in ("user", "admin", "system_admin"):
        assert '+ 업로드' in _index_html(f"{role}@x.com", role)


def _sample_doc() -> dict:
    """render_index 가 읽는 최소 문서 딕셔너리(공개 버튼은 카드가 있어야 렌더된다)."""
    return {"title": "t", "href": "/proposals/a/index.html", "rel": "proposals/a/index.html",
            "group": "proposals", "mtime": 1000.0, "size_kb": 10, "pdf": None, "pending_pdf": False}


def test_index_share_button_sysadmin_only():
    # 공개(share) 버튼은 system_admin(can_share)만. 카드 유무 의존을 피해 순수 함수로 검증.
    # 'card-share' 는 항상 있는 CSS 규칙과도 매칭되므로 버튼 전용 마커(data-doc-rel)로 판정한다.
    docs = [_sample_doc()]
    assert 'data-doc-rel=' in server.render_index(docs, "s", can_share=True)
    assert 'data-doc-rel=' not in server.render_index(docs, "u", can_share=False)
