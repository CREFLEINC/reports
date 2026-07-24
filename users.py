"""
users.py — 파일 기반 사용자 저장소 + 역할 유틸(순수 모듈).

server.py 가 import 한다. 순환을 피하려고 server 를 import 하지 않으며 설정은 환경변수에서
독립적으로 읽는다(shares.py·uploads_handler.py 와 동일 원칙). 저장은 uploads/ 볼륨(gitignore +
:rw 바인드 마운트)의 JSON 파일에 하고, 쓰기는 tmp → os.replace 로 원자적이다. 비밀번호는
shares.py 의 stdlib pbkdf2 해시를 재사용한다(신규 의존성 없음, 평문 미저장).

역할 체계(서열 높은 순): system_admin > admin > user > viewer.
레거시 env 계정 토큰의 role 클레임('uploader'/'reader')은 normalize_role 로 4역할에 매핑한다
(uploader→system_admin, reader→viewer). 저장소 사용자는 4역할 문자열을 그대로 쓴다.

레코드 스키마(JSON object, key=email(소문자 정규화)):
  {email, role, active, pw_salt, pw_hash, created_at, updated_at}
"""
from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
from pathlib import Path

from shares import hash_password, verify_password

BASE_DIR = Path(__file__).resolve().parent
UPLOADS_DIR = (BASE_DIR / os.environ.get("REPORTS_UPLOADS_DIR", "uploads")).resolve()
# 테스트는 이 전역을 직접 덮어써 임시 파일로 격리한다(users.USERS_FILE = ...). shares.py 와 동일 패턴.
USERS_FILE = Path(
    os.environ.get("REPORTS_USERS_FILE", str(UPLOADS_DIR / "users.json"))
).resolve()

# 역할 서열: 값이 클수록 강한 권한. normalize_role 을 거친 4역할만 키로 갖는다.
_ROLE_RANK = {"viewer": 0, "user": 1, "admin": 2, "system_admin": 3}
ROLES = ("system_admin", "admin", "user", "viewer")
# 레거시 와이어 role → 4역할(uploader=쓰기 최상위, reader=열람 전용).
_LEGACY_ROLE_MAP = {"uploader": "system_admin", "reader": "viewer"}
# 토큰/Basic 에서 허용하는 role 문자열(정규화 전). 이 밖의 값은 미인증으로 취급.
KNOWN_ROLES = frozenset(_ROLE_RANK) | frozenset(_LEGACY_ROLE_MAP)

# 간단한 email 형식 검증(로컬@도메인.tld). 엄격한 RFC 파싱은 목표가 아니다.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_LOCK = threading.Lock()  # 읽기-수정-쓰기 보호(동기 라우트는 스레드풀 실행)


# ──────────────────────────────────────────────────────────────────────────
# 역할 유틸 (조각 2·3 이 재사용)
# ──────────────────────────────────────────────────────────────────────────
def normalize_role(role: str) -> str:
    """role 문자열을 4역할 체계로 정규화. 4역할은 항등, 레거시(uploader/reader)는 매핑.

    알 수 없는 값이면 ValueError. 호출 전 KNOWN_ROLES 로 게이트하면 발생하지 않는다.
    """
    if role in _ROLE_RANK:
        return role
    if role in _LEGACY_ROLE_MAP:
        return _LEGACY_ROLE_MAP[role]
    raise ValueError(f"알 수 없는 역할: {role!r}")


def role_rank(role: str) -> int:
    """정규화된 역할의 서열 값(클수록 강함). viewer=0 … system_admin=3."""
    return _ROLE_RANK[normalize_role(role)]


def role_at_least(role: str, minimum: str) -> bool:
    """role 의 서열이 minimum 이상인지. 능력 판정(예: admin 이상)에 사용."""
    return role_rank(role) >= role_rank(minimum)


# ──────────────────────────────────────────────────────────────────────────
# email · 역할 검증
# ──────────────────────────────────────────────────────────────────────────
def _normalize_email(email: str) -> str:
    """소문자·공백 제거 정규화(형식 검증은 하지 않음)."""
    return (email or "").strip().lower()


def _require_valid_email(email: str) -> None:
    if not _EMAIL_RE.match(email):
        raise ValueError("이메일 형식이 올바르지 않습니다.")


def _reserved_emails() -> set[str]:
    """env 계정(REPORTS_USER/REPORTS_UPLOAD_USER) 값 — 저장소 등록을 거부할 예약 email."""
    reserved = set()
    for var, default in (("REPORTS_USER", "crefle"), ("REPORTS_UPLOAD_USER", "crefle")):
        val = os.environ.get(var, default)
        if val:
            reserved.add(val.strip().lower())
    return reserved


# ──────────────────────────────────────────────────────────────────────────
# 저장소 (JSON, 원자적 쓰기) — shares.py 패턴 재사용
# ──────────────────────────────────────────────────────────────────────────
def _read_raw() -> dict:
    try:
        with USERS_FILE.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError):
        # 손상 파일은 빈 상태로 취급(서비스 지속) — 다음 쓰기에서 복구된다.
        return {}


def _write_raw(data: dict) -> None:
    USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = USERS_FILE.with_name(f".{USERS_FILE.name}.{secrets.token_hex(6)}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, USERS_FILE)


def public_view(rec: dict) -> dict:
    """해시·비밀번호를 제외한 외부 공개용 사용자 뷰."""
    return {
        "email": rec["email"],
        "role": rec["role"],
        "active": rec["active"],
        "created_at": rec["created_at"],
        "updated_at": rec["updated_at"],
    }


# ──────────────────────────────────────────────────────────────────────────
# CRUD
# ──────────────────────────────────────────────────────────────────────────
def add_user(email: str, password: str, role: str) -> dict:
    """사용자 등록. pbkdf2 해시만 저장(평문 미저장). 등록된 public_view 반환.

    형식 오류 email·알 수 없는 역할·빈 비밀번호·env 예약 email·중복 등록 시 ValueError.
    """
    email = _normalize_email(email)
    _require_valid_email(email)
    role = normalize_role(role)
    if not password:
        raise ValueError("비밀번호를 입력하세요.")
    if email in _reserved_emails():
        raise ValueError("시스템 계정 이메일은 등록할 수 없습니다.")
    salt, digest = hash_password(password)
    now = time.time()
    with _LOCK:
        data = _read_raw()
        if email in data:
            raise ValueError("이미 등록된 이메일입니다.")
        rec = {
            "email": email,
            "role": role,
            "active": True,
            "pw_salt": salt,
            "pw_hash": digest,
            "created_at": now,
            "updated_at": now,
        }
        data[email] = rec
        _write_raw(data)
        return public_view(rec)


def get_user(email: str) -> dict | None:
    """저장소 사용자 레코드(해시 포함) 복사본 또는 None. 형식 검증 없이 조회만."""
    email = _normalize_email(email)
    if not email:
        return None
    with _LOCK:
        rec = _read_raw().get(email)
    return dict(rec) if rec else None


def list_users() -> list[dict]:
    """등록순(created_at) public_view 목록. 해시·비밀번호 미노출."""
    with _LOCK:
        data = _read_raw()
    return [public_view(r) for r in sorted(data.values(), key=lambda r: r.get("created_at", 0))]


def verify_credentials(email: str, password: str) -> str | None:
    """자격증명 검증. 성공 시 저장된 4역할, 실패/정지/부재면 None. active=False 는 인증 거부."""
    rec = get_user(email)
    if rec is None or not rec.get("active"):
        return None
    if verify_password(password, rec.get("pw_salt") or "", rec.get("pw_hash") or ""):
        return rec["role"]
    return None


def update_user(
    email: str,
    *,
    password: str | None = None,
    role: str | None = None,
    active: bool | None = None,
) -> dict:
    """비밀번호·역할·active 를 선택적으로 갱신. 갱신된 public_view 반환.

    email(ID)은 불변이며 계정 삭제는 없다(정지로 대체). 없는 사용자·빈 비밀번호·
    알 수 없는 역할이면 ValueError.
    """
    email = _normalize_email(email)
    if password is not None and not password:
        raise ValueError("비밀번호를 비울 수 없습니다.")
    normalized_role = normalize_role(role) if role is not None else None
    new_hash = hash_password(password) if password is not None else None
    with _LOCK:
        data = _read_raw()
        rec = data.get(email)
        if rec is None:
            raise ValueError("존재하지 않는 사용자입니다.")
        if new_hash is not None:
            rec["pw_salt"], rec["pw_hash"] = new_hash
        if normalized_role is not None:
            rec["role"] = normalized_role
        if active is not None:
            rec["active"] = bool(active)
        rec["updated_at"] = time.time()
        data[email] = rec
        _write_raw(data)
        return public_view(rec)


def set_active(email: str, active: bool) -> dict:
    """계정 정지/활성화 토글. 갱신된 public_view 반환(없는 사용자면 ValueError)."""
    return update_user(email, active=active)
