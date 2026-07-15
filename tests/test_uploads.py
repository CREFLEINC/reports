"""업로드 ZIP 처리 — macOS/Windows 메타데이터 정리 + 단일 래핑 폴더 평탄화.

_extract_zip_safe / _flatten_single_root 는 경로 인자를 받으므로 tmp_path 로 직접 단위
테스트한다(비동기 불필요). 마지막 한 건은 TestClient 로 POST /upload 전 경로를 e2e 검증.
"""
import os
import zipfile
from pathlib import Path

# server import 전에 결정적 자격증명/키 강제(test_auth 와 동일 — 단독 실행도 동작하도록).
os.environ.setdefault("REPORTS_USER", "reader")
os.environ.setdefault("REPORTS_PASS", "readerpass")
os.environ.setdefault("REPORTS_UPLOAD_USER", "uploader")
os.environ.setdefault("REPORTS_UPLOAD_PASS", "uploaderpass")
os.environ.setdefault("REPORTS_SECRET_KEY", "test-secret-deadbeef-0123456789abcdef")

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import server
import uploads_handler as uh
from server import app

HTML = b"<!doctype html><title>t</title><p>ok</p>"


def _make_zip(path: Path, members: dict) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return path


# ── _is_junk_member ────────────────────────────────────────────────────────
@pytest.mark.parametrize("name", [
    "__MACOSX/._index.html", "__MACOSX/assets/._a.css", ".DS_Store",
    "sub/.DS_Store", "._index.html", ".fseventsd/x", "Thumbs.db", "desktop.ini",
    "assets/Thumbs.db",
])
def test_is_junk_true(name):
    assert uh._is_junk_member(name) is True


@pytest.mark.parametrize("name", [
    "index.html", "assets/style.css", "deck.js", "a/b/c.png", "fonts/x.woff2",
])
def test_is_junk_false(name):
    assert uh._is_junk_member(name) is False


# ── _extract_zip_safe: 정크 스킵 ────────────────────────────────────────────
def test_dsstore_no_longer_rejected(tmp_path):
    z = _make_zip(tmp_path / "u.zip", {"index.html": HTML, ".DS_Store": b"\x00\x01junk"})
    stage = tmp_path / "stage"
    uh._extract_zip_safe(z, stage)  # 거부되면 안 됨
    assert (stage / "index.html").is_file()
    assert not (stage / ".DS_Store").exists()


def test_macosx_appledouble_skipped(tmp_path):
    z = _make_zip(tmp_path / "u.zip", {
        "index.html": HTML,
        "assets/style.css": b"body{}",
        "__MACOSX/._index.html": b"\x00\x05appledouble",
        "__MACOSX/assets/._style.css": b"\x00\x05appledouble",
    })
    stage = tmp_path / "stage"
    uh._extract_zip_safe(z, stage)
    assert (stage / "index.html").is_file()
    assert (stage / "assets" / "style.css").is_file()
    assert not (stage / "__MACOSX").exists()


def test_windows_junk_skipped(tmp_path):
    z = _make_zip(tmp_path / "u.zip", {
        "index.html": HTML, "Thumbs.db": b"\x00thumb", "desktop.ini": b"[.ShellClassInfo]",
    })
    stage = tmp_path / "stage"
    uh._extract_zip_safe(z, stage)
    assert (stage / "index.html").is_file()
    assert not (stage / "Thumbs.db").exists()
    assert not (stage / "desktop.ini").exists()


def test_all_junk_zip_rejected_clearly(tmp_path):
    z = _make_zip(tmp_path / "u.zip", {".DS_Store": b"\x00", "__MACOSX/._x.html": b"\x00"})
    stage = tmp_path / "stage"
    with pytest.raises(HTTPException) as ei:
        uh._extract_zip_safe(z, stage)
    assert ei.value.status_code == 422
    assert "콘텐츠" in ei.value.detail


def test_traversal_in_junk_still_rejected(tmp_path):
    # 보안 회귀 가드: __MACOSX 트리라도 .. traversal 은 여전히 거부되어야 한다.
    z = _make_zip(tmp_path / "u.zip", {"index.html": HTML, "__MACOSX/../../evil.html": HTML})
    stage = tmp_path / "stage"
    with pytest.raises(HTTPException) as ei:
        uh._extract_zip_safe(z, stage)
    assert ei.value.status_code == 422


# ── _flatten_single_root ────────────────────────────────────────────────────
def test_flatten_single_wrapping_folder(tmp_path):
    stage = tmp_path / "stage"
    (stage / "mydoc" / "assets").mkdir(parents=True)
    (stage / "mydoc" / "index.html").write_bytes(HTML)
    (stage / "mydoc" / "assets" / "style.css").write_bytes(b"body{}")
    uh._flatten_single_root(stage)
    assert (stage / "index.html").is_file()
    assert (stage / "assets" / "style.css").is_file()
    assert not (stage / "mydoc").exists()


def test_flatten_noop_when_index_at_top(tmp_path):
    stage = tmp_path / "stage"
    (stage / "assets").mkdir(parents=True)
    (stage / "index.html").write_bytes(HTML)
    (stage / "assets" / "style.css").write_bytes(b"body{}")
    uh._flatten_single_root(stage)  # 정상 구조는 건드리지 않음
    assert (stage / "index.html").is_file()
    assert (stage / "assets" / "style.css").is_file()


def test_flatten_nested_double_wrap(tmp_path):
    stage = tmp_path / "stage"
    (stage / "a" / "b").mkdir(parents=True)
    (stage / "a" / "b" / "index.html").write_bytes(HTML)
    uh._flatten_single_root(stage)
    assert (stage / "index.html").is_file()


def test_flatten_child_named_like_holding_no_crash(tmp_path):
    # 래핑 폴더 X 안에 X__lift 같은 자식이 있어도 rename 충돌/500 없이 평탄화돼야 한다.
    stage = tmp_path / "stage"
    (stage / "report" / "report__lift").mkdir(parents=True)
    (stage / "report" / "report__lift" / "index.html").write_bytes(HTML)
    uh._flatten_single_root(stage)
    assert (stage / "index.html").is_file()


def test_flatten_deep_wrapping(tmp_path):
    # 단일 폴더가 깊게(>10단계) 중첩돼도 index.html 을 최상위로 끌어올린다.
    stage = tmp_path / "stage"
    deep = stage
    for i in range(15):
        deep = deep / f"d{i}"
    deep.mkdir(parents=True)
    (deep / "index.html").write_bytes(HTML)
    uh._flatten_single_root(stage)
    assert (stage / "index.html").is_file()


def test_flatten_noop_when_no_html_inside(tmp_path):
    # 단일 디렉터리지만 평탄화해도 html 이 없으면 그냥 끌어올리기만 한다(거부는 resolve 가).
    stage = tmp_path / "stage"
    (stage / "assets").mkdir(parents=True)
    (stage / "assets" / "style.css").write_bytes(b"body{}")
    uh._flatten_single_root(stage)
    assert (stage / "style.css").is_file()


# ── 추출 + 평탄화 + resolve 통합 (Finder '폴더 압축' 형태) ────────────────────
def test_finder_folder_compress_pipeline(tmp_path):
    z = _make_zip(tmp_path / "u.zip", {
        "mydoc/index.html": HTML,
        "mydoc/assets/style.css": b"body{}",
        "mydoc/.DS_Store": b"\x00",
        "__MACOSX/mydoc/._index.html": b"\x00",
        "__MACOSX/mydoc/assets/._style.css": b"\x00",
    })
    stage = tmp_path / "stage"
    uh._extract_zip_safe(z, stage)
    uh._flatten_single_root(stage)
    uh._resolve_doc_html(stage)
    assert (stage / "index.html").is_file()
    assert (stage / "assets" / "style.css").is_file()
    assert not (stage / "__MACOSX").exists()


# ── e2e: POST /upload 로 Finder zip 게시 ─────────────────────────────────────
@pytest.fixture
def cleanup_published():
    dest = server.UPLOADS_DOCS / "demo" / "macostest_v1"
    before = set(uh.QUEUE_DIR.glob("*.json")) if uh.QUEUE_DIR.is_dir() else set()
    yield dest
    import shutil
    shutil.rmtree(dest, ignore_errors=True)
    if uh.QUEUE_DIR.is_dir():
        for j in set(uh.QUEUE_DIR.glob("*.json")) - before:
            j.unlink(missing_ok=True)


def test_upload_macos_finder_zip_publishes(tmp_path, cleanup_published):
    dest = cleanup_published
    z = _make_zip(tmp_path / "u.zip", {
        "macostest/index.html": HTML,
        "macostest/assets/style.css": b"body{}",
        "macostest/.DS_Store": b"\x00",
        "__MACOSX/macostest/._index.html": b"\x00",
    })
    c = TestClient(app)
    c.cookies.set("reports_token", server._make_token("uploader", "uploader"))
    with (tmp_path / "u.zip").open("rb") as fh:
        r = c.post(
            "/upload",
            data={"doc_type": "demo", "name": "macostest", "version": "1"},
            files={"file": ("archive.zip", fh, "application/zip")},
        )
    assert r.status_code == 201, r.text
    assert (dest / "index.html").is_file()
    assert (dest / "assets" / "style.css").is_file()
    assert not (dest / "__MACOSX").exists()
    assert not (dest / ".DS_Store").exists()


# ── e2e: POST /api/v1/documents (핸들러 재사용 API 래퍼) ─────────────────────
def _uploader_client() -> TestClient:
    """uploader JWT 쿠키를 실은 TestClient (기존 e2e 테스트와 동일 인증 방식)."""
    c = TestClient(app)
    c.cookies.set("reports_token", server._make_token("uploader", "uploader"))
    return c


@pytest.fixture
def cleanup_api_docs():
    """API e2e 정리: apitest 유형 폴더 전체 + 이 테스트가 만든 렌더 큐 잡 제거."""
    type_dir = server.UPLOADS_DOCS / "apitest"
    before = set(uh.QUEUE_DIR.glob("*.json")) if uh.QUEUE_DIR.is_dir() else set()
    yield type_dir
    import shutil
    shutil.rmtree(type_dir, ignore_errors=True)
    if uh.QUEUE_DIR.is_dir():
        for j in set(uh.QUEUE_DIR.glob("*.json")) - before:
            j.unlink(missing_ok=True)


def test_api_documents_html_publishes(cleanup_api_docs):
    # 단일 .html → 201, 핸들러 반환 + 정규화 type/name/version 에코 + Location 헤더, 디스크 게시.
    type_dir = cleanup_api_docs
    c = _uploader_client()
    r = c.post(
        "/api/v1/documents",
        data={"doc_type": "apitest", "name": "apidoc", "version": "1"},
        files={"file": ("report.html", HTML, "text/html")},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "published"
    assert body["pdf_pending"] is True
    assert body["href"] == "/uploads/docs/apitest/apidoc_v1/index.html"
    assert body["type"] == "apitest"
    assert body["name"] == "apidoc"
    assert body["version"] == "1"
    assert r.headers["location"] == body["href"]
    assert (type_dir / "apidoc_v1" / "index.html").is_file()


def test_api_documents_zip_publishes(tmp_path, cleanup_api_docs):
    # .zip(HTML + 자산) → 201, index.html + 자산 게시.
    type_dir = cleanup_api_docs
    z = _make_zip(tmp_path / "u.zip", {"index.html": HTML, "assets/style.css": b"body{}"})
    c = _uploader_client()
    with z.open("rb") as fh:
        r = c.post(
            "/api/v1/documents",
            data={"doc_type": "apitest", "name": "apizip", "version": "2"},
            files={"file": ("bundle.zip", fh, "application/zip")},
        )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "published"
    assert body["href"] == "/uploads/docs/apitest/apizip_v2/index.html"
    assert r.headers["location"] == body["href"]
    assert (type_dir / "apizip_v2" / "index.html").is_file()
    assert (type_dir / "apizip_v2" / "assets" / "style.css").is_file()


def test_api_documents_normalizes_echo(cleanup_api_docs):
    # 핸들러 내부 정규화와 동일하게 라우트가 type/name/version 을 정규화해 에코한다.
    c = _uploader_client()
    r = c.post(
        "/api/v1/documents",
        data={"doc_type": "APITEST", "name": "  My Doc ", "version": "v3"},
        files={"file": ("report.html", HTML, "text/html")},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["type"] == "apitest"
    assert body["name"] == "My_Doc"
    assert body["version"] == "3"
    assert body["href"] == "/uploads/docs/apitest/My_Doc_v3/index.html"
    assert r.headers["location"] == body["href"]


def test_api_documents_duplicate_and_overwrite(cleanup_api_docs):
    # 중복(overwrite 미지정) → 409, overwrite=1 → 재게시 201 (핸들러 상속 동작 + overwrite 필드).
    c = _uploader_client()
    fields = {"doc_type": "apitest", "name": "apidup", "version": "1"}
    r1 = c.post("/api/v1/documents", data=fields,
                files={"file": ("report.html", HTML, "text/html")})
    assert r1.status_code == 201, r1.text
    r2 = c.post("/api/v1/documents", data=fields,
                files={"file": ("report.html", HTML, "text/html")})
    assert r2.status_code == 409, r2.text
    r3 = c.post("/api/v1/documents", data={**fields, "overwrite": "1"},
                files={"file": ("report.html", HTML, "text/html")})
    assert r3.status_code == 201, r3.text


def test_api_documents_bad_extension_415():
    # 허용 안 된 확장자 → 415 (핸들러 상속). 게시물 없음 → 정리 불필요.
    c = _uploader_client()
    r = c.post(
        "/api/v1/documents",
        data={"doc_type": "apitest", "name": "apibad", "version": "1"},
        files={"file": ("notes.txt", b"hello", "text/plain")},
    )
    assert r.status_code == 415, r.text


def test_api_documents_unauthenticated_401():
    # 인증 없이(require_uploader) → 401.
    c = TestClient(app)
    r = c.post(
        "/api/v1/documents",
        headers={"accept": "application/json"},
        data={"doc_type": "apitest", "name": "apidoc", "version": "1"},
        files={"file": ("report.html", HTML, "text/html")},
    )
    assert r.status_code == 401, r.text
