"""
shares.py — 자료별 public 공개 링크 저장·검증(순수 모듈).

server.py 가 import 한다. 순환을 피하려고 server 를 import 하지 않으며 설정은 환경변수에서 독립적으로
읽는다(uploads_handler.py 와 동일 원칙). 저장은 uploads/ 볼륨(gitignore + :rw 바인드 마운트,
ops/backup-uploads.sh 백업 대상)의 JSON 파일에 하고, 쓰기는 tmp → os.replace 로 원자적이다.
비밀번호는 stdlib pbkdf2 해시로만 저장한다(신규 의존성 도입 없음).

레코드 스키마(JSON object, key=token):
  {token, doc_rel, doc_dir, title, has_password, pw_salt, pw_hash,
   expiry_epoch, created_at, created_by}
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
UPLOADS_DIR = (BASE_DIR / os.environ.get("REPORTS_UPLOADS_DIR", "uploads")).resolve()
# 테스트는 이 전역을 직접 덮어써 임시 파일로 격리한다(shares.SHARES_FILE = ...).
SHARES_FILE = Path(
    os.environ.get("REPORTS_SHARES_FILE", str(UPLOADS_DIR / "shares.json"))
).resolve()

PBKDF2_ALG = "sha256"
PBKDF2_ITER = 200_000
SALT_BYTES = 16
TOKEN_BYTES = 32
MAX_SHARE_DAYS = 366  # 공개 기간 상한(서버 검증)

_LOCK = threading.Lock()  # 읽기-수정-쓰기 보호(동기 라우트는 스레드풀 실행)


# ──────────────────────────────────────────────────────────────────────────
# 비밀번호 해시 (stdlib pbkdf2 — 신규 의존성 없음)
# ──────────────────────────────────────────────────────────────────────────
def hash_password(password: str) -> tuple[str, str]:
    """(salt_hex, hash_hex) 반환."""
    salt = secrets.token_bytes(SALT_BYTES)
    dk = hashlib.pbkdf2_hmac(PBKDF2_ALG, password.encode("utf-8"), salt, PBKDF2_ITER)
    return salt.hex(), dk.hex()


def verify_password(password: str, salt_hex: str, hash_hex: str) -> bool:
    """상수시간 비교. salt/hash 가 비었거나 깨지면 False."""
    if not salt_hex or not hash_hex:
        return False
    try:
        salt = bytes.fromhex(salt_hex)
    except ValueError:
        return False
    dk = hashlib.pbkdf2_hmac(PBKDF2_ALG, password.encode("utf-8"), salt, PBKDF2_ITER)
    return secrets.compare_digest(dk.hex(), hash_hex)


# ──────────────────────────────────────────────────────────────────────────
# 만료
# ──────────────────────────────────────────────────────────────────────────
def compute_expiry(date_str: str) -> float:
    """'YYYY-MM-DD'(마감일) → 그 날 끝(23:59:59 로컬)의 epoch."""
    try:
        d = datetime.strptime((date_str or "").strip(), "%Y-%m-%d")
    except (ValueError, TypeError):
        raise ValueError("만료일 형식이 올바르지 않습니다(YYYY-MM-DD).")
    return d.replace(hour=23, minute=59, second=59, microsecond=0).timestamp()


def validate_expiry(expiry_epoch: float) -> None:
    now = time.time()
    if expiry_epoch <= now:
        raise ValueError("만료일은 미래여야 합니다.")
    if expiry_epoch > now + MAX_SHARE_DAYS * 86400:
        raise ValueError(f"공개 기간은 최대 {MAX_SHARE_DAYS}일까지 가능합니다.")


def is_expired(record: dict, *, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    return float(record.get("expiry_epoch", 0)) <= now


# ──────────────────────────────────────────────────────────────────────────
# 저장소 (JSON, 원자적 쓰기)
# ──────────────────────────────────────────────────────────────────────────
def _read_raw() -> dict:
    try:
        with SHARES_FILE.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError):
        # 손상 파일은 빈 상태로 취급(서비스 지속) — 다음 쓰기에서 복구된다.
        return {}


def _write_raw(data: dict) -> None:
    SHARES_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SHARES_FILE.with_name(f".{SHARES_FILE.name}.{secrets.token_hex(6)}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, SHARES_FILE)


def _prune_expired(data: dict) -> dict:
    now = time.time()
    return {t: r for t, r in data.items() if float(r.get("expiry_epoch", 0)) > now}


def load_shares() -> dict:
    """만료분을 제거한 전체 맵을 반환(변경 시 파일도 정리)."""
    with _LOCK:
        data = _read_raw()
        pruned = _prune_expired(data)
        if len(pruned) != len(data):
            _write_raw(pruned)
        return pruned


def get_share(token: str) -> dict | None:
    """유효(미만료) 레코드 또는 None. 만료분은 지연 정리한다."""
    if not token:
        return None
    with _LOCK:
        data = _read_raw()
        rec = data.get(token)
        if rec is None:
            return None
        if is_expired(rec):
            del data[token]
            _write_raw(data)
            return None
        return rec


def create_share(*, doc_rel: str, doc_dir: str, title: str, password: str | None,
                  expiry_epoch: float, created_by: str) -> dict:
    validate_expiry(expiry_epoch)
    has_password = bool(password)
    pw_salt = pw_hash = None
    if has_password:
        pw_salt, pw_hash = hash_password(password)
    with _LOCK:
        data = _prune_expired(_read_raw())
        token = secrets.token_urlsafe(TOKEN_BYTES)
        while token in data:  # 사실상 불가능하나 충돌 가드
            token = secrets.token_urlsafe(TOKEN_BYTES)
        rec = {
            "token": token, "doc_rel": doc_rel, "doc_dir": doc_dir, "title": title,
            "has_password": has_password, "pw_salt": pw_salt, "pw_hash": pw_hash,
            "expiry_epoch": expiry_epoch, "created_at": time.time(), "created_by": created_by,
        }
        data[token] = rec
        _write_raw(data)
        return rec


def delete_share(token: str) -> bool:
    with _LOCK:
        data = _read_raw()
        if token in data:
            del data[token]
            _write_raw(data)
            return True
        return False


def find_active_by_doc(doc_rel: str) -> dict | None:
    """해당 자료의 활성 공개 중 가장 최근 1건(모달 재오픈 시 기존 링크 표시용)."""
    with _LOCK:
        data = _prune_expired(_read_raw())
        matches = [r for r in data.values() if r.get("doc_rel") == doc_rel]
        if not matches:
            return None
        return max(matches, key=lambda r: r.get("created_at", 0))
