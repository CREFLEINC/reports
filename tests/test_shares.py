"""자료별 public 공개 기능 테스트.

- 단위: pbkdf2 해시/검증, 만료 계산·경계, doc_rel 화이트리스트.
- 통합(TestClient): 권한(uploader 전용), 무인증 공개 접근(열람/PDF), 비번 보호, 만료, 해제,
  형제 문서 차단·공유 에셋 허용.
- 목표(PASS): 실제 5개 자료에 대해 '문서 열람'과 'PDF 다운로드' 2가지 모두 200 을 단언한다.

저장은 shares.SHARES_FILE 전역을 임시 파일로 덮어써 격리하고, 매 테스트 전에 비운다.
"""
from __future__ import annotations  # Python 3.9: `str | None` 등 어노테이션 지연 평가

import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

# server import 전에 결정적 자격증명/키 강제(test_auth 와 동일 — 단독 실행도 동작).
os.environ.setdefault("REPORTS_USER", "reader")
os.environ.setdefault("REPORTS_PASS", "readerpass")
os.environ.setdefault("REPORTS_UPLOAD_USER", "uploader")
os.environ.setdefault("REPORTS_UPLOAD_PASS", "uploaderpass")
os.environ.setdefault("REPORTS_SECRET_KEY", "test-secret-deadbeef-0123456789abcdef")

import pytest
from fastapi.testclient import TestClient

import server
import shares
from server import app

# 다른 테스트 모듈이 이미 server/shares 를 import 했어도 안전하도록 전역을 직접 덮어쓴다.
shares.SHARES_FILE = Path(server.BASE_DIR) / "uploads" / "tmp" / "test_shares.json"


def _expiry(days: int = 30) -> str:
    return (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")


@pytest.fixture(autouse=True)
def _clean_shares():
    shares.SHARES_FILE.parent.mkdir(parents=True, exist_ok=True)
    shares._write_raw({})
    yield
    shares.SHARES_FILE.unlink(missing_ok=True)


def _uploader_client() -> TestClient:
    c = TestClient(app)
    c.cookies.set("reports_token", server._make_token("uploader", "uploader"))
    return c


def _reader_client() -> TestClient:
    c = TestClient(app)
    c.cookies.set("reports_token", server._make_token("reader", "reader"))
    return c


def _create_share(doc_rel: str, *, password: str | None = None, days: int = 30) -> dict:
    c = _uploader_client()
    r = c.post("/api/share", json={
        "doc_rel": doc_rel,
        "use_password": bool(password),
        "password": password or "",
        "expiry_date": _expiry(days),
    })
    assert r.status_code == 201, r.text
    return r.json()


# 실제 자료 선택(커밋된 .pdf 보유 문서) ─────────────────────────────────────
def _docs_with_pdf():
    return [d for d in server.discover_documents() if d.get("pdf")]


def _ohmyfactory_doc():
    return next(d for d in server.discover_documents()
                if d["rel"].startswith("proposals/ohmyfactory/") and d["rel"].endswith(".html"))


# ── 단위: 비밀번호 해시 ──────────────────────────────────────────────────────
def test_password_hash_roundtrip():
    salt, h = shares.hash_password("hunter2")
    assert salt and h and h != "hunter2"
    assert shares.verify_password("hunter2", salt, h) is True
    assert shares.verify_password("WRONG", salt, h) is False


def test_verify_password_handles_empty_and_garbage():
    assert shares.verify_password("x", "", "") is False
    assert shares.verify_password("x", "zzz", "qqq") is False  # 비-hex salt


# ── 단위: 만료 ──────────────────────────────────────────────────────────────
def test_compute_expiry_end_of_day():
    e = shares.compute_expiry("2099-01-02")
    dt = datetime.fromtimestamp(e)
    assert (dt.year, dt.month, dt.day, dt.hour, dt.minute) == (2099, 1, 2, 23, 59)


def test_compute_expiry_bad_format():
    with pytest.raises(ValueError):
        shares.compute_expiry("not-a-date")


def test_validate_expiry_bounds():
    now = time.time()
    with pytest.raises(ValueError):
        shares.validate_expiry(now - 10)                 # 과거
    with pytest.raises(ValueError):
        shares.validate_expiry(now + 400 * 86400)        # 상한 초과
    shares.validate_expiry(now + 30 * 86400)             # 정상 — 예외 없어야 함


def test_create_share_unique_tokens():
    doc = _docs_with_pdf()[0]["rel"]
    a = _create_share(doc)
    b = _create_share(doc)
    assert a["token"] != b["token"]


# ── 통합: 생성 권한 ──────────────────────────────────────────────────────────
def test_create_requires_auth():
    r = TestClient(app).post("/api/share", json={
        "doc_rel": _docs_with_pdf()[0]["rel"], "use_password": False,
        "password": "", "expiry_date": _expiry()})
    assert r.status_code == 401


def test_create_forbidden_for_reader():
    r = _reader_client().post("/api/share", json={
        "doc_rel": _docs_with_pdf()[0]["rel"], "use_password": False,
        "password": "", "expiry_date": _expiry()})
    assert r.status_code == 403


def test_create_rejects_non_document_path():
    for bad in ("../server.py", "server.py", "uploads/audit.log"):
        r = _uploader_client().post("/api/share", json={
            "doc_rel": bad, "use_password": False, "password": "", "expiry_date": _expiry()})
        assert r.status_code == 404, bad


def test_create_rejects_password_flag_without_value():
    r = _uploader_client().post("/api/share", json={
        "doc_rel": _docs_with_pdf()[0]["rel"], "use_password": True,
        "password": "  ", "expiry_date": _expiry()})
    assert r.status_code == 422


def test_create_rejects_past_expiry():
    r = _uploader_client().post("/api/share", json={
        "doc_rel": _docs_with_pdf()[0]["rel"], "use_password": False,
        "password": "", "expiry_date": _expiry(-5)})
    assert r.status_code == 422


# ── 통합: 무인증 공개 접근 ───────────────────────────────────────────────────
def test_public_landing_no_auth():
    data = _create_share(_docs_with_pdf()[0]["rel"])
    r = TestClient(app).get(f"/s/{data['token']}")
    assert r.status_code == 200
    assert "문서 열람" in r.text and "PDF 다운로드" in r.text


def test_public_view_serves_html_no_auth():
    doc = _docs_with_pdf()[0]
    data = _create_share(doc["rel"])
    r = TestClient(app).get(f"/s/{data['token']}/view/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_view_redirect_adds_trailing_slash():
    data = _create_share(_docs_with_pdf()[0]["rel"])
    r = TestClient(app).get(f"/s/{data['token']}/view", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"].endswith("/view/")


def test_public_pdf_download_no_auth():
    data = _create_share(_docs_with_pdf()[0]["rel"])
    r = TestClient(app).get(f"/s/{data['token']}/pdf")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert "attachment" in r.headers.get("content-disposition", "")


# ── 통합: 공유 에셋 허용 + 형제 문서 차단 (ohmyfactory) ───────────────────────
def test_shared_assets_served_but_sibling_docs_blocked():
    doc = _ohmyfactory_doc()
    data = _create_share(doc["rel"])
    tok = data["token"]
    pub = TestClient(app)
    # 공유 에셋(같은 디렉터리)은 열람 허용
    assert pub.get(f"/s/{tok}/view/colors_and_type.css").status_code == 200
    assert pub.get(f"/s/{tok}/view/assets/crefle-mark.svg").status_code == 200
    # 같은 디렉터리의 다른 .html(형제 문서)은 차단
    sibling = next(d for d in server.discover_documents()
                   if d["rel"].startswith("proposals/ohmyfactory/")
                   and d["rel"].endswith(".html") and d["rel"] != doc["rel"])
    name = Path(sibling["rel"]).name
    assert pub.get(f"/s/{tok}/view/{quote(name)}").status_code == 404
    # traversal 시도 차단
    assert pub.get(f"/s/{tok}/view/../../server.py").status_code == 404


# ── 통합: 비밀번호 보호 ──────────────────────────────────────────────────────
def test_password_protected_flow():
    doc = _docs_with_pdf()[0]
    data = _create_share(doc["rel"], password="s3cret")
    tok = data["token"]
    assert data["has_password"] is True

    # 랜딩 = 비번 폼, 잠금해제 전 열람/PDF 는 랜딩으로 리다이렉트
    pub = TestClient(app)
    assert "비밀번호" in pub.get(f"/s/{tok}").text
    assert pub.get(f"/s/{tok}/view/", follow_redirects=False).status_code == 303
    assert pub.get(f"/s/{tok}/pdf", follow_redirects=False).status_code == 303

    # 오답 거부(쿠키 미발급)
    bad = TestClient(app).post(f"/s/{tok}/unlock", data={"password": "nope"},
                               follow_redirects=False)
    assert bad.status_code == 401
    assert "올바르지" in bad.text

    # 정답 → 쿠키 → 열람/PDF 허용
    c = TestClient(app)
    ok = c.post(f"/s/{tok}/unlock", data={"password": "s3cret"}, follow_redirects=False)
    assert ok.status_code == 303
    assert "share_unlock=" in ok.headers.get("set-cookie", "")
    assert c.get(f"/s/{tok}/view/").status_code == 200
    assert c.get(f"/s/{tok}/pdf").status_code == 200


# ── 통합: 만료 / 해제 ────────────────────────────────────────────────────────
def test_expired_share_not_accessible():
    doc = _docs_with_pdf()[0]
    rec = {
        "token": "expiredtok", "doc_rel": doc["rel"],
        "doc_dir": str(Path(doc["rel"]).parent.as_posix()), "title": doc["title"],
        "has_password": False, "pw_salt": None, "pw_hash": None,
        "expiry_epoch": time.time() - 10, "created_at": time.time() - 100, "created_by": "uploader",
    }
    data = shares._read_raw(); data["expiredtok"] = rec; shares._write_raw(data)
    pub = TestClient(app)
    assert pub.get("/s/expiredtok").status_code == 404
    assert pub.get("/s/expiredtok/view/", follow_redirects=False).status_code == 404
    assert pub.get("/s/expiredtok/pdf", follow_redirects=False).status_code == 404


def test_revoke_disables_link():
    doc = _docs_with_pdf()[0]
    data = _create_share(doc["rel"])
    tok = data["token"]
    assert TestClient(app).get(f"/s/{tok}").status_code == 200
    d = _uploader_client().delete(f"/api/share/{tok}")
    assert d.status_code == 204
    assert TestClient(app).get(f"/s/{tok}").status_code == 404


def test_current_share_lookup_for_modal():
    doc = _docs_with_pdf()[0]
    c = _uploader_client()
    # 없을 때
    assert c.get("/api/share", params={"doc": doc["rel"]}).json() == {"active": False}
    created = _create_share(doc["rel"])
    got = c.get("/api/share", params={"doc": doc["rel"]}).json()
    assert got["active"] is True and got["token"] == created["token"]


# ── 인덱스 UI: 공개 버튼 노출 권한 ───────────────────────────────────────────
def test_index_shows_share_button_for_uploader_only():
    # 버튼 마크업은 data-doc-rel 속성으로 식별(`.card-share` 클래스명은 CSS 에 항상 존재).
    up = _uploader_client().get("/", headers={"accept": "text/html"})
    assert up.status_code == 200
    assert "data-doc-rel=" in up.text and "shareModal" in up.text

    rd = _reader_client().get("/", headers={"accept": "text/html"})
    assert rd.status_code == 200
    assert "data-doc-rel=" not in rd.text and "shareModal" not in rd.text


# ── 목표(PASS): 실제 5개 자료 — 열람 + PDF 다운로드 2가지 모두 ───────────────
def _e2e_docs():
    docs = _docs_with_pdf()
    assert len(docs) >= 5, f"PDF 보유 자료가 5개 미만입니다: {len(docs)}"
    return docs[:5]


@pytest.mark.parametrize("doc", _e2e_docs(), ids=lambda d: d["rel"])
def test_e2e_public_view_and_pdf_pass(doc):
    data = _create_share(doc["rel"])
    tok = data["token"]
    pub = TestClient(app)

    view = pub.get(f"/s/{tok}/view/")
    assert view.status_code == 200, f"열람 실패: {doc['rel']}"
    assert "text/html" in view.headers["content-type"]

    pdf = pub.get(f"/s/{tok}/pdf")
    assert pdf.status_code == 200, f"PDF 다운로드 실패: {doc['rel']}"
    assert pdf.headers["content-type"] == "application/pdf"
    assert len(pdf.content) > 0
