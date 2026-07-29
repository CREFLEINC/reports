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


def test_share_unlock_locks_on_fifth_failure_and_rejects_valid_password(monkeypatch):
    now = [500.0]
    monkeypatch.setattr(server.time, "monotonic", lambda: now[0])
    token = _create_share(_docs_with_pdf()[0]["rel"], password="s3cret")["token"]
    c = TestClient(app, client=("203.0.113.10", 50000))

    for _ in range(4):
        failed = c.post(f"/s/{token}/unlock", data={"password": "WRONG"})
        assert failed.status_code == 401

    locked = c.post(f"/s/{token}/unlock", data={"password": "WRONG"})
    assert locked.status_code == 429
    assert locked.headers["retry-after"] == "60"

    now[0] = 501.2
    valid = c.post(f"/s/{token}/unlock", data={"password": "s3cret"})
    assert valid.status_code == 429
    assert valid.headers["retry-after"] == "59"


def test_share_unlock_failure_buckets_are_isolated_by_ip_and_token(monkeypatch):
    monkeypatch.setattr(server.time, "monotonic", lambda: 600.0)
    doc_rel = _docs_with_pdf()[0]["rel"]
    first_token = _create_share(doc_rel, password="first-pass")["token"]
    second_token = _create_share(doc_rel, password="second-pass")["token"]
    locked_client = TestClient(app, client=("203.0.113.11", 50000))
    other_client = TestClient(app, client=("203.0.113.12", 50000))

    for _ in range(5):
        locked_client.post(f"/s/{first_token}/unlock", data={"password": "WRONG"})

    same_ip_other_share = locked_client.post(
        f"/s/{second_token}/unlock",
        data={"password": "second-pass"},
        follow_redirects=False,
    )
    other_ip_same_share = other_client.post(
        f"/s/{first_token}/unlock",
        data={"password": "first-pass"},
        follow_redirects=False,
    )
    assert same_ip_other_share.status_code == 303
    assert other_ip_same_share.status_code == 303


def test_share_unlock_lock_expires_and_success_resets_failures(monkeypatch):
    now = [700.0]
    monkeypatch.setattr(server.time, "monotonic", lambda: now[0])
    token = _create_share(_docs_with_pdf()[0]["rel"], password="s3cret")["token"]
    c = TestClient(app, client=("203.0.113.13", 50000))

    for _ in range(4):
        failed = c.post(f"/s/{token}/unlock", data={"password": "WRONG"})
        assert failed.status_code == 401
    success = c.post(
        f"/s/{token}/unlock",
        data={"password": "s3cret"},
        follow_redirects=False,
    )
    assert success.status_code == 303

    for _ in range(4):
        failed = c.post(f"/s/{token}/unlock", data={"password": "WRONG"})
        assert failed.status_code == 401

    locked = c.post(f"/s/{token}/unlock", data={"password": "WRONG"})
    assert locked.status_code == 429

    now[0] = 760.0
    after_expiry = c.post(
        f"/s/{token}/unlock",
        data={"password": "s3cret"},
        follow_redirects=False,
    )
    assert after_expiry.status_code == 303


def test_share_unlock_rate_limit_skips_missing_and_unprotected_shares(monkeypatch):
    monkeypatch.setattr(server.time, "monotonic", lambda: 800.0)
    c = TestClient(app, client=("203.0.113.14", 50000))

    for _ in range(6):
        assert c.post("/s/missing/unlock", data={"password": "WRONG"}).status_code == 404

    token = _create_share(_docs_with_pdf()[0]["rel"])["token"]
    for _ in range(6):
        response = c.post(
            f"/s/{token}/unlock",
            data={"password": "anything"},
            follow_redirects=False,
        )
        assert response.status_code == 303


def test_partial_share_failures_expire_after_sixty_idle_seconds(monkeypatch):
    now = [1100.0]
    monkeypatch.setattr(server.time, "monotonic", lambda: now[0])
    token = _create_share(_docs_with_pdf()[0]["rel"], password="s3cret")["token"]
    c = TestClient(app, client=("203.0.113.15", 50000))

    for _ in range(4):
        failed = c.post(f"/s/{token}/unlock", data={"password": "WRONG"})
        assert failed.status_code == 401

    now[0] = 1160.0
    after_expiry = c.post(f"/s/{token}/unlock", data={"password": "WRONG"})
    assert after_expiry.status_code == 401


def test_share_failure_bucket_fails_closed_without_evicting_active_entries(monkeypatch):
    now = [1200.0]
    monkeypatch.setattr(server.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(server, "_FAILURE_BUCKET_LIMIT", 3, raising=False)
    token = _create_share(_docs_with_pdf()[0]["rel"], password="s3cret")["token"]

    victim = TestClient(app, client=("203.0.113.1", 50000))
    for _ in range(5):
        victim.post(f"/s/{token}/unlock", data={"password": "WRONG"})
    now[0] = 1201.0
    for suffix in (2, 3):
        attempt_client = TestClient(app, client=(f"203.0.113.{suffix}", 50000))
        failed = attempt_client.post(f"/s/{token}/unlock", data={"password": "WRONG"})
        assert failed.status_code == 401

    before_churn = dict(server._SHARE_UNLOCK_FAILURES)
    now[0] = 1202.0
    for suffix in (4, 5, 6):
        attempt_client = TestClient(app, client=(f"203.0.113.{suffix}", 50000))
        saturated = attempt_client.post(f"/s/{token}/unlock", data={"password": "WRONG"})
        assert saturated.status_code == 429
        assert saturated.headers["retry-after"] == "58"

    assert len(server._SHARE_UNLOCK_FAILURES) == 3
    assert server._SHARE_UNLOCK_FAILURES == before_churn
    locked = victim.post(f"/s/{token}/unlock", data={"password": "s3cret"})
    assert locked.status_code == 429


def test_saturated_share_bucket_allows_valid_password(monkeypatch):
    monkeypatch.setattr(server.time, "monotonic", lambda: 1250.0)
    monkeypatch.setattr(server, "_FAILURE_BUCKET_LIMIT", 3, raising=False)
    token = _create_share(_docs_with_pdf()[0]["rel"], password="s3cret")["token"]

    for suffix in (1, 2, 3):
        attempt_client = TestClient(app, client=(f"203.0.113.{suffix}", 50000))
        failed = attempt_client.post(f"/s/{token}/unlock", data={"password": "WRONG"})
        assert failed.status_code == 401

    before_success = dict(server._SHARE_UNLOCK_FAILURES)
    valid_new_key = TestClient(app, client=("203.0.113.7", 50000)).post(
        f"/s/{token}/unlock",
        data={"password": "s3cret"},
        follow_redirects=False,
    )
    assert valid_new_key.status_code == 303
    assert server._SHARE_UNLOCK_FAILURES == before_success


def test_saturated_share_overflow_limits_ip_after_interleaved_success(
    monkeypatch,
):
    monkeypatch.setattr(server.time, "monotonic", lambda: 1300.0)
    monkeypatch.setattr(server, "_FAILURE_BUCKET_LIMIT", 2, raising=False)
    doc_rel = _docs_with_pdf()[0]["rel"]
    tokens = [_create_share(doc_rel, password="s3cret")["token"] for _ in range(7)]

    for suffix in (1, 2):
        attempt_client = TestClient(app, client=(f"203.0.113.{suffix}", 50000))
        failed = attempt_client.post(
            f"/s/{tokens[0]}/unlock",
            data={"password": "WRONG"},
        )
        assert failed.status_code == 401

    before_overflow = dict(server._SHARE_UNLOCK_FAILURES)
    verified_passwords = []
    verify_password = shares.verify_password

    def count_verification(password, salt_hex, hash_hex):
        verified_passwords.append(password)
        return verify_password(password, salt_hex, hash_hex)

    monkeypatch.setattr(shares, "verify_password", count_verification)
    overflow_client = TestClient(app, client=("203.0.113.50", 50000))
    for token in tokens[:4]:
        response = overflow_client.post(
            f"/s/{token}/unlock",
            data={"password": "WRONG"},
        )
        assert response.status_code == 429

    valid = overflow_client.post(
        f"/s/{tokens[4]}/unlock",
        data={"password": "s3cret"},
        follow_redirects=False,
    )
    assert valid.status_code == 303
    fifth_failure = overflow_client.post(
        f"/s/{tokens[5]}/unlock",
        data={"password": "WRONG"},
    )
    assert fifth_failure.status_code == 429
    assert verified_passwords.count("WRONG") == 5

    before_locked_attempt = list(verified_passwords)
    locked = overflow_client.post(
        f"/s/{tokens[6]}/unlock",
        data={"password": "WRONG"},
    )
    assert locked.status_code == 429
    assert locked.headers["retry-after"] == "60"
    assert verified_passwords == before_locked_attempt
    assert server._SHARE_UNLOCK_FAILURES == before_overflow


def test_share_overflow_counter_survives_primary_slot_release_until_expiry(
    monkeypatch,
):
    now = [1450.0]
    monkeypatch.setattr(server.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(server, "_FAILURE_BUCKET_LIMIT", 1, raising=False)
    doc_rel = _docs_with_pdf()[0]["rel"]
    tokens = [_create_share(doc_rel, password="s3cret")["token"] for _ in range(7)]
    attack_client = TestClient(app, client=("203.0.113.70", 50000))

    primary_failure = attack_client.post(
        f"/s/{tokens[0]}/unlock",
        data={"password": "WRONG"},
    )
    assert primary_failure.status_code == 401

    verified_passwords = []
    verify_password = shares.verify_password

    def count_verification(password, salt_hex, hash_hex):
        verified_passwords.append(password)
        return verify_password(password, salt_hex, hash_hex)

    monkeypatch.setattr(shares, "verify_password", count_verification)
    for token in tokens[1:5]:
        overflow_failure = attack_client.post(
            f"/s/{token}/unlock",
            data={"password": "WRONG"},
        )
        assert overflow_failure.status_code == 429

    valid = attack_client.post(
        f"/s/{tokens[0]}/unlock",
        data={"password": "s3cret"},
        follow_redirects=False,
    )
    assert valid.status_code == 303
    assert server._SHARE_UNLOCK_FAILURES == {}

    fifth_failure = attack_client.post(
        f"/s/{tokens[5]}/unlock",
        data={"password": "WRONG"},
    )
    assert fifth_failure.status_code == 429
    assert fifth_failure.headers["retry-after"] == "60"
    assert verified_passwords.count("WRONG") == 5
    assert server._SHARE_UNLOCK_FAILURE_OVERFLOW["203.0.113.70"][0] == 5
    assert server._SHARE_UNLOCK_FAILURES == {}

    before_locked_attempt = list(verified_passwords)
    locked = attack_client.post(
        f"/s/{tokens[6]}/unlock",
        data={"password": "WRONG"},
    )
    assert locked.status_code == 429
    assert verified_passwords == before_locked_attempt

    now[0] = 1510.0
    after_expiry = attack_client.post(
        f"/s/{tokens[6]}/unlock",
        data={"password": "WRONG"},
    )
    assert after_expiry.status_code == 401
    assert verified_passwords.count("WRONG") == 6
    assert "203.0.113.70" not in server._SHARE_UNLOCK_FAILURE_OVERFLOW
    assert ("203.0.113.70", tokens[6]) in server._SHARE_UNLOCK_FAILURES


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
