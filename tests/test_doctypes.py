"""문서 유형 레지스트리(doctypes.py) 및 관리 API 테스트.

- 단위: 시드, etc 보장, add/rename/delete, 예외(중복·etc 보호·미존재), 정규화.
- 통합(TestClient): /api/types CRUD 권한·중복 409·count·삭제 시 문서 기타 이동·공유 재지정,
  업로드 폼/인덱스 라벨 반영.

격리: doctypes.TYPES_FILE·doctypes.UPLOADS_DOCS(및 server 측 동일 전역)를 tmp 로 덮어쓴다.
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

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
from server import app

# 격리 디렉터리는 BASE_DIR 하위여야 한다(_move 가 relative_to(BASE_DIR) 사용 — test_shares 관례).
_TMP = (Path(server.BASE_DIR) / "uploads" / "tmp" / "test_doctypes").resolve()


@pytest.fixture(autouse=True)
def _iso():
    # 다른 테스트 모듈에 누수되지 않도록 원래 전역을 저장 후 복원한다.
    orig = (doctypes.TYPES_FILE, doctypes.UPLOADS_DOCS, server.UPLOADS_DOCS, shares.SHARES_FILE)
    shutil.rmtree(_TMP, ignore_errors=True)
    docs = (_TMP / "docs").resolve()
    docs.mkdir(parents=True, exist_ok=True)
    doctypes.TYPES_FILE = (_TMP / "types.json").resolve()
    doctypes.UPLOADS_DOCS = docs
    server.UPLOADS_DOCS = docs
    shares.SHARES_FILE = (_TMP / "shares.json").resolve()
    shares._write_raw({})
    yield
    doctypes.TYPES_FILE, doctypes.UPLOADS_DOCS, server.UPLOADS_DOCS, shares.SHARES_FILE = orig
    shutil.rmtree(_TMP, ignore_errors=True)


def _uploader() -> TestClient:
    c = TestClient(app)
    c.cookies.set("reports_token", server._make_token("uploader", "uploader"))
    return c


def _reader() -> TestClient:
    c = TestClient(app)
    c.cookies.set("reports_token", server._make_token("reader", "reader"))
    return c


def _slugs():
    return [t["slug"] for t in doctypes.load_types()]


# ── 시드 ────────────────────────────────────────────────────────────────
def test_seed_creates_defaults_and_etc_last():
    types = doctypes.load_types()
    assert [t["slug"] for t in types] == ["proposal", "demo", "ohmyfactory", "etc"]
    assert types[-1] == {"slug": "etc", "label": "기타", "builtin": True}
    assert doctypes.TYPES_FILE.exists()


def test_seed_merges_existing_folders():
    (doctypes.UPLOADS_DOCS / "legacy").mkdir()
    slugs = _slugs()
    assert "legacy" in slugs and slugs[-1] == "etc"


# ── add ─────────────────────────────────────────────────────────────────
def test_add_inserts_before_etc_and_normalizes():
    rec = doctypes.add_type("Report", "보고서")
    assert rec == {"slug": "report", "label": "보고서", "builtin": False}
    assert _slugs() == ["proposal", "demo", "ohmyfactory", "report", "etc"]


def test_add_duplicate_raises():
    doctypes.add_type("report", "보고서")
    with pytest.raises(ValueError, match="이미 존재"):
        doctypes.add_type("report", "다른이름")


def test_add_existing_seed_slug_raises():
    with pytest.raises(ValueError, match="이미 존재"):
        doctypes.add_type("demo", "데모2")


def test_add_invalid_slug_raises():
    with pytest.raises(ValueError):
        doctypes.add_type("한글슬러그", "x")
    with pytest.raises(ValueError):
        doctypes.add_type("-bad", "x")


def test_add_empty_label_raises():
    with pytest.raises(ValueError):
        doctypes.add_type("ok", "   ")


# ── rename ──────────────────────────────────────────────────────────────
def test_rename_changes_label_only():
    doctypes.add_type("report", "보고서")
    rec = doctypes.rename_type("report", "리포트")
    assert rec["label"] == "리포트"
    assert doctypes.label_for("report") == "리포트"


def test_rename_etc_blocked():
    with pytest.raises(ValueError):
        doctypes.rename_type("etc", "기타아님")


def test_rename_missing_raises():
    with pytest.raises(ValueError):
        doctypes.rename_type("nope", "x")


# ── delete ──────────────────────────────────────────────────────────────
def test_delete_removes_from_registry():
    doctypes.add_type("report", "보고서")
    doctypes.delete_type("report")
    assert "report" not in _slugs()


def test_delete_etc_blocked():
    with pytest.raises(ValueError):
        doctypes.delete_type("etc")


def test_delete_missing_raises():
    with pytest.raises(ValueError):
        doctypes.delete_type("nope")


# ── 불변식/헬퍼 ──────────────────────────────────────────────────────────
def test_etc_always_present_even_if_file_tampered():
    doctypes._write_raw([{"slug": "only", "label": "only", "builtin": False}])
    assert "etc" in _slugs()
    assert doctypes.label_for("etc") == "기타"


def test_helpers():
    doctypes.add_type("report", "보고서")
    assert doctypes.type_exists("report") is True
    assert doctypes.type_exists("REPORT") is True
    assert doctypes.type_exists("nope") is False
    assert doctypes.type_exists("한글") is False
    assert doctypes.label_for("nope") is None


# ── API 권한 ─────────────────────────────────────────────────────────────
def test_api_requires_uploader():
    assert _reader().get("/api/types").status_code in (401, 403)
    assert _reader().post("/api/types", json={"slug": "x", "label": "엑스"}).status_code in (401, 403)
    assert TestClient(app).get("/api/types").status_code in (401, 403)
    assert _reader().get("/types").status_code in (401, 403)


def test_api_list_has_seed_and_count():
    (doctypes.UPLOADS_DOCS / "demo" / "d1_v1").mkdir(parents=True)
    r = _uploader().get("/api/types")
    assert r.status_code == 200
    data = r.json()
    assert [t["slug"] for t in data][-1] == "etc"
    assert next(t for t in data if t["slug"] == "demo")["count"] == 1
    assert next(t for t in data if t["slug"] == "etc")["builtin"] is True


def test_api_create_and_duplicate_409():
    c = _uploader()
    r = c.post("/api/types", json={"slug": "report", "label": "보고서"})
    assert r.status_code == 201 and r.json()["slug"] == "report"
    r2 = c.post("/api/types", json={"slug": "report", "label": "또보고서"})
    assert r2.status_code == 409 and "이미 존재" in r2.json()["detail"]


def test_api_create_invalid_slug_422():
    assert _uploader().post("/api/types", json={"slug": "한글", "label": "x"}).status_code == 422


def test_api_rename_ok_and_guards():
    c = _uploader()
    c.post("/api/types", json={"slug": "report", "label": "보고서"})
    r = c.patch("/api/types/report", json={"label": "리포트"})
    assert r.status_code == 200 and r.json()["label"] == "리포트"
    assert doctypes.label_for("report") == "리포트"
    assert c.patch("/api/types/etc", json={"label": "x"}).status_code == 422
    assert c.patch("/api/types/nope", json={"label": "x"}).status_code == 404


def test_api_delete_moves_docs_to_etc_and_rebases_share():
    c = _uploader()
    c.post("/api/types", json={"slug": "report", "label": "보고서"})
    doc = doctypes.UPLOADS_DOCS / "report" / "a_v1"
    doc.mkdir(parents=True)
    (doc / "index.html").write_text("<title>A</title>", encoding="utf-8")
    (doctypes.UPLOADS_DOCS / "report" / "b_v1").mkdir(parents=True)
    (doctypes.UPLOADS_DOCS / "etc" / "a_v1").mkdir(parents=True)  # 이름 충돌 유발

    rel_dir = doc.resolve().relative_to(server.BASE_DIR).as_posix()
    shares.create_share(doc_rel=rel_dir + "/index.html", doc_dir=rel_dir, title="A",
                        password=None, expiry_epoch=time.time() + 86400, created_by="uploader")

    r = c.delete("/api/types/report")
    assert r.status_code == 200 and r.json()["moved"] == 2
    assert "report" not in _slugs()
    assert (doctypes.UPLOADS_DOCS / "etc" / "a_v1_2" / "index.html").is_file()  # 충돌 → _2
    assert (doctypes.UPLOADS_DOCS / "etc" / "b_v1").is_dir()
    assert not (doctypes.UPLOADS_DOCS / "report").exists()

    new_dir = (doctypes.UPLOADS_DOCS / "etc" / "a_v1_2").resolve().relative_to(server.BASE_DIR).as_posix()
    rec = next(iter(shares._read_raw().values()))
    assert rec["doc_dir"] == new_dir
    assert rec["doc_rel"] == new_dir + "/index.html"


def test_api_delete_guards():
    assert _uploader().delete("/api/types/etc").status_code == 400
    assert _uploader().delete("/api/types/nope").status_code == 404


# ── 통합: 폼·그룹 라벨 반영 ───────────────────────────────────────────────
def test_upload_form_reflects_registry():
    doctypes.add_type("report", "보고서")
    page = server.render_upload_form()
    assert '<option value="report">보고서</option>' in page
    assert 'value="etc">기타</option>' in page
    assert 'href="/types"' in page


def test_group_label_uses_registry():
    doctypes.add_type("report", "보고서")
    assert server._group_label("uploads/report") == "보고서"
    assert server._group_label("uploads/etc") == "기타"
    assert server._group_label("uploads/unknown") == "업로드 · unknown"
