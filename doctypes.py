"""
doctypes.py — 업로드 문서 유형 레지스트리(순수 모듈).

(모듈명은 stdlib `types` 를 가리지 않도록 `doctypes` 로 둔다.)

server.py 가 import 한다. 순환을 피하려 server 를 import 하지 않고 설정은 환경변수에서 독립적으로 읽는다
(shares.py / uploads_handler.py 동일 원칙). 저장은 uploads/ 볼륨의 JSON 배열에 하고, 쓰기는
tmp→os.replace 로 원자적이다. 유형은 {slug(폴더 키), label(표시명), builtin} 레코드이며 순서를 보존한다.

내장 유형 'etc'(기타)는 항상 존재하며 삭제·이름변경 불가하다(삭제된 유형에 속한 문서의 fallback).
문서 폴더 이동(삭제 시 기타로 일괄 이동)은 파일시스템·공유와 얽히므로 server.py 가 조율하고,
이 모듈은 레지스트리(JSON)만 담당한다.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import threading
import unicodedata
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
UPLOADS_DIR = (BASE_DIR / os.environ.get("REPORTS_UPLOADS_DIR", "uploads")).resolve()
# 테스트는 이 전역들을 직접 덮어써 임시 경로로 격리한다(types.TYPES_FILE = ..., types.UPLOADS_DOCS = ...).
UPLOADS_DOCS = UPLOADS_DIR / "docs"
TYPES_FILE = Path(
    os.environ.get("REPORTS_TYPES_FILE", str(UPLOADS_DIR / "types.json"))
).resolve()

BUILTIN_SLUG = "etc"
BUILTIN_LABEL = "기타"
LABEL_MAX = 40

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,30}$")
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")

# 최초 시드 기본 유형(기존 하드코딩 유형 → 한글 라벨).
_SEED_DEFAULTS = [
    ("proposal", "제안서"),
    ("demo", "데모"),
    ("ohmyfactory", "OhMyFactory"),
]

_LOCK = threading.Lock()  # 읽기-수정-쓰기 보호(동기 라우트는 스레드풀 실행)


# ──────────────────────────────────────────────────────────────────────────
# 검증/정규화
# ──────────────────────────────────────────────────────────────────────────
def normalize_slug(slug: str) -> str:
    s = unicodedata.normalize("NFC", (slug or "")).strip().lower()
    if not _SLUG_RE.match(s):
        raise ValueError("슬러그는 영문 소문자·숫자·_- (첫 글자 영숫자, 31자 이내)여야 합니다.")
    return s


def normalize_label(label: str) -> str:
    s = _CTRL_RE.sub("", unicodedata.normalize("NFC", (label or "")).strip())
    if not s:
        raise ValueError("유형 이름을 입력하세요.")
    if len(s) > LABEL_MAX:
        raise ValueError(f"유형 이름은 {LABEL_MAX}자 이내여야 합니다.")
    return s


def _rec(slug: str, label: str, builtin: bool = False) -> dict:
    return {"slug": slug, "label": label, "builtin": builtin}


# ──────────────────────────────────────────────────────────────────────────
# 저장소 (JSON 배열, 원자적 쓰기)
# ──────────────────────────────────────────────────────────────────────────
def _read_raw():
    """list 반환. 파일 없으면 None(시드 트리거), 손상되면 [](다음 쓰기에서 복구)."""
    try:
        with TYPES_FILE.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError):
        return []


def _write_raw(data: list) -> None:
    TYPES_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = TYPES_FILE.with_name(f".{TYPES_FILE.name}.{secrets.token_hex(6)}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, TYPES_FILE)


def _discover_folder_slugs() -> list:
    """uploads/docs/ 아래 실재하는 유형 폴더 슬러그(시드 병합용)."""
    out = []
    if UPLOADS_DOCS.is_dir():
        for child in sorted(UPLOADS_DOCS.iterdir()):
            if child.is_dir() and _SLUG_RE.match(child.name):
                out.append(child.name)
    return out


def _seed() -> list:
    seen = set()
    data = []
    for slug, label in _SEED_DEFAULTS:
        data.append(_rec(slug, label))
        seen.add(slug)
    for slug in _discover_folder_slugs():
        if slug not in seen and slug != BUILTIN_SLUG:
            data.append(_rec(slug, slug))
            seen.add(slug)
    data.append(_rec(BUILTIN_SLUG, BUILTIN_LABEL, builtin=True))
    return data


def _ensure_builtin(data: list) -> list:
    """etc(기타)가 항상 존재하고 builtin/label 이 고정되도록 보정."""
    if not any(t.get("slug") == BUILTIN_SLUG for t in data):
        data.append(_rec(BUILTIN_SLUG, BUILTIN_LABEL, builtin=True))
    else:
        for t in data:
            if t.get("slug") == BUILTIN_SLUG:
                t["builtin"] = True
                t["label"] = BUILTIN_LABEL
    return data


def _load_locked() -> list:
    """_LOCK 보유 상태에서 호출. 시드/보정 후 정규 리스트 반환(변경 시 파일도 갱신)."""
    raw = _read_raw()
    if raw is None:
        data = _seed()
        _write_raw(data)
        return data
    fixed = _ensure_builtin([dict(t) for t in raw if isinstance(t, dict) and t.get("slug")])
    if fixed != raw:
        _write_raw(fixed)
    return fixed


# ──────────────────────────────────────────────────────────────────────────
# 공개 API (순수 — 레지스트리만)
# ──────────────────────────────────────────────────────────────────────────
def load_types() -> list:
    """유형 레코드 리스트(순서 보존, etc 포함)."""
    with _LOCK:
        return [dict(t) for t in _load_locked()]


def add_type(slug: str, label: str) -> dict:
    """새 유형 추가. 슬러그 중복이면 ValueError(이미 존재하는 유형)."""
    slug = normalize_slug(slug)
    label = normalize_label(label)
    with _LOCK:
        data = _load_locked()
        if any(t["slug"] == slug for t in data):
            raise ValueError("이미 존재하는 유형입니다.")
        rec = _rec(slug, label)
        idx = next((i for i, t in enumerate(data) if t["slug"] == BUILTIN_SLUG), len(data))
        data.insert(idx, rec)  # etc 는 항상 마지막에 유지
        _write_raw(data)
        return dict(rec)


def rename_type(slug: str, new_label: str) -> dict:
    """유형의 표시 라벨 변경(슬러그/폴더 불변). etc/미존재면 ValueError."""
    slug = normalize_slug(slug)
    new_label = normalize_label(new_label)
    with _LOCK:
        data = _load_locked()
        for t in data:
            if t["slug"] == slug:
                if t.get("builtin"):
                    raise ValueError("기본 유형(기타)은 이름을 변경할 수 없습니다.")
                t["label"] = new_label
                _write_raw(data)
                return dict(t)
        raise ValueError("존재하지 않는 유형입니다.")


def delete_type(slug: str) -> None:
    """레지스트리에서 유형 제거. etc/미존재면 ValueError. (문서 이동은 server 가 수행.)"""
    slug = normalize_slug(slug)
    with _LOCK:
        data = _load_locked()
        target = next((t for t in data if t["slug"] == slug), None)
        if target is None:
            raise ValueError("존재하지 않는 유형입니다.")
        if target.get("builtin"):
            raise ValueError("기본 유형(기타)은 삭제할 수 없습니다.")
        _write_raw([t for t in data if t["slug"] != slug])


def type_exists(slug: str) -> bool:
    try:
        slug = normalize_slug(slug)
    except ValueError:
        return False
    return any(t["slug"] == slug for t in load_types())


def label_for(slug: str) -> str | None:
    for t in load_types():
        if t["slug"] == slug:
            return t["label"]
    return None
