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
