"""
uploads_handler — 웹 업로드 처리(검증 · zip 안전 · 원자적 게시 · 렌더 작업 enqueue · 감사).

server.py 가 POST /upload 에서 호출한다. server 를 import 하지 않아(순환 방지) 설정은 환경변수에서
독립적으로 읽는다(BASE_DIR 는 같은 /app). 모든 결정적·보안 로직은 pathlib/zipfile 로 구현하며
register_report.sh 같은 외부 셸을 호출하지 않는다(주입면 차단).

게시 트리: <UPLOADS_DIR>/docs/<type>/<name>_v<ver>/index.html (+ 자산 + index.pdf[워커 생성])
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat as statmod
import time
import unicodedata
import uuid
import zipfile
from pathlib import Path

from fastapi import HTTPException, UploadFile

BASE_DIR = Path(__file__).resolve().parent
UPLOADS_DIR = (BASE_DIR / os.environ.get("REPORTS_UPLOADS_DIR", "uploads")).resolve()
UPLOADS_DOCS = UPLOADS_DIR / "docs"
QUEUE_DIR = UPLOADS_DIR / "queue"
TMP_DIR = UPLOADS_DIR / "tmp"
AUDIT_LOG = UPLOADS_DIR / "audit.log"

MAX_UPLOAD = int(os.environ.get("REPORTS_MAX_UPLOAD_MB", "50")) * 1024 * 1024
PER_FILE_MAX = MAX_UPLOAD                      # zip 내 개별 파일 압축해제 상한
TOTAL_UNCOMPRESSED_MAX = 200 * 1024 * 1024     # zip 누적 압축해제 상한(zip-bomb 방어)
MAX_ENTRIES = 2000
MAX_RATIO = 200                                # 개별 압축비 상한
MIN_FREE_BYTES = 500 * 1024 * 1024             # 디스크 여유 watermark

# 서빙 가능한 확장자(server.MEDIA_TYPES 와 동일 집합). zip 멤버 화이트리스트로도 사용.
ALLOWED_EXT = {
    ".html", ".htm", ".css", ".js", ".json", ".md", ".pdf", ".svg",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".woff2", ".woff", ".otf", ".ttf",
}

_BIDI = {0x202A, 0x202B, 0x202C, 0x202D, 0x202E, 0x2066, 0x2067, 0x2068, 0x2069}
_TYPE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,30}$")
_NAME_ALLOWED = re.compile(r"[^A-Za-z0-9 가-힣ㄱ-ㅎㅏ-ㅣ._-]")
_VER_ALLOWED = re.compile(r"[^0-9A-Za-z._-]")


def _bad(msg: str, code: int = 422):
    raise HTTPException(status_code=code, detail=msg)


def _clean_text(s: str) -> str:
    s = unicodedata.normalize("NFC", s or "")
    if any(ord(c) in _BIDI for c in s):
        _bad("이름/버전에 허용되지 않는 제어문자(BIDI)가 있습니다.")
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in s):
        _bad("이름/버전에 제어문자가 있습니다.")
    return s


def _safe_name(name: str) -> str:
    s = _clean_text(name).strip()
    s = _NAME_ALLOWED.sub("", s)
    s = re.sub(r"\s+", "_", s).strip("._-")
    s = s[:80].strip("._-")
    if not s:
        _bad("이름이 비었거나 허용 문자가 없습니다.")
    return s


def _safe_version(version: str) -> str:
    s = _clean_text(version).strip()
    s = s[1:] if s[:1] in ("v", "V") else s
    s = _VER_ALLOWED.sub("", s)[:20].strip("._-")
    if not s:
        _bad("버전이 비었거나 허용 문자가 없습니다.")
    return s


def _safe_type(doc_type: str) -> str:
    s = unicodedata.normalize("NFC", (doc_type or "")).strip().lower()
    if not _TYPE_RE.match(s):
        _bad("문서 유형은 영문 슬러그(a-z0-9_-, 31자 이내)여야 합니다.")
    return s


def _within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _ensure_dirs() -> None:
    for d in (UPLOADS_DOCS, QUEUE_DIR, QUEUE_DIR / "done", TMP_DIR):
        d.mkdir(parents=True, exist_ok=True)


async def _stream_to(file: UploadFile, dest: Path) -> str:
    """업로드를 dest 로 스트리밍(크기 상한 강제). sha256 반환."""
    h = hashlib.sha256()
    total = 0
    with dest.open("wb") as out:
        while True:
            chunk = await file.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_UPLOAD:
                out.close()
                dest.unlink(missing_ok=True)
                _bad(f"업로드 크기 상한({MAX_UPLOAD // (1024*1024)}MB) 초과.", 413)
            h.update(chunk)
            out.write(chunk)
    if total == 0:
        dest.unlink(missing_ok=True)
        _bad("빈 파일입니다.")
    return h.hexdigest()


def _extract_zip_safe(zip_path: Path, stage: Path) -> None:
    """zip 을 stage 로 안전 추출(extractall 미사용). zip-slip/bomb/symlink/확장자 방어."""
    stage.mkdir(parents=True, exist_ok=True)
    total = 0
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile:
        _bad("올바른 zip 파일이 아닙니다.")
    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_ENTRIES:
            _bad(f"zip 항목 수({len(infos)})가 상한({MAX_ENTRIES})을 초과합니다.")
        for zi in infos:
            nm = zi.filename
            if nm.endswith("/"):
                continue  # 디렉터리 엔트리는 필요 시 자동 생성
            if "\x00" in nm or nm.startswith("/") or nm.startswith("\\") or "\\" in nm or ":" in nm:
                _bad("zip 멤버 경로가 안전하지 않습니다(절대경로/구분자).")
            if ".." in Path(nm).parts:
                _bad("zip 멤버에 상위경로(..)가 있습니다.")
            mode = (zi.external_attr >> 16) & 0xFFFF
            if statmod.S_ISLNK(mode):
                _bad("zip 내 심볼릭링크는 허용되지 않습니다.")
            ext = Path(nm).suffix.lower()
            if ext not in ALLOWED_EXT:
                _bad(f"허용되지 않는 확장자: {ext or '(없음)'} ({nm})")
            if zi.file_size > PER_FILE_MAX:
                _bad(f"zip 내 파일이 너무 큽니다: {nm}")
            if zi.compress_size > 0 and zi.file_size / zi.compress_size > MAX_RATIO:
                _bad(f"압축비가 비정상적으로 높습니다(zip-bomb 의심): {nm}")
            target = (stage / nm).resolve()
            if not _within(target, stage):
                _bad("zip-slip 차단: 추출 경로가 범위를 벗어납니다.")
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(zi) as src, target.open("wb") as out:
                while True:
                    chunk = src.read(65536)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > TOTAL_UNCOMPRESSED_MAX:
                        _bad("zip 누적 압축해제 크기 상한 초과(zip-bomb 의심).")
                    out.write(chunk)


def _resolve_doc_html(stage: Path) -> None:
    """stage 루트에 index.html 을 보장. 없으면 단일 top-level .html 을 index.html 로."""
    if (stage / "index.html").is_file():
        return
    htmls = [p for p in stage.iterdir() if p.is_file() and p.suffix.lower() in (".html", ".htm")]
    if len(htmls) == 1:
        htmls[0].rename(stage / "index.html")
        return
    _bad("zip 에 index.html 또는 단일 .html 이 있어야 합니다.")


def _enqueue_render(rel_dir: str) -> None:
    """uploads/queue/<uuid>.json 작업 생성(원자적 write)."""
    job = {"rel": rel_dir, "html": "index.html", "created": time.time(), "attempts": 0}
    jid = uuid.uuid4().hex
    tmp = QUEUE_DIR / f".{jid}.json.tmp"
    tmp.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, QUEUE_DIR / f"{jid}.json")


def _audit(ip: str, uploader: str, doc_type: str, name: str, version: str, sha: str, rel: str) -> None:
    line = json.dumps(
        {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "ip": ip, "uploader": uploader,
         "type": doc_type, "name": name, "version": version, "sha256": sha, "path": rel},
        ensure_ascii=False,
    )
    with AUDIT_LOG.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


async def handle_upload(*, file: UploadFile, doc_type: str, name: str, version: str,
                        client_ip: str, uploader: str, overwrite: bool) -> dict:
    _ensure_dirs()
    if shutil.disk_usage(UPLOADS_DIR).free < MIN_FREE_BYTES:
        _bad("서버 디스크 여유가 부족합니다.", 507)

    doc_type = _safe_type(doc_type)
    name = _safe_name(name)
    version = _safe_version(version)

    fn = (file.filename or "").lower()
    if fn.endswith((".html", ".htm")):
        kind = "html"
    elif fn.endswith(".zip"):
        kind = "zip"
    else:
        _bad("업로드는 .html 또는 .zip 만 허용됩니다.", 415)

    dest_dir = (UPLOADS_DOCS / doc_type / f"{name}_v{version}").resolve()
    if not _within(dest_dir, UPLOADS_DOCS):
        _bad("대상 경로가 업로드 범위를 벗어납니다.")
    if dest_dir.exists() and not overwrite:
        _bad(f"이미 존재합니다: {doc_type}/{name}_v{version} (버전을 올리거나 덮어쓰기 선택).", 409)

    work_id = uuid.uuid4().hex
    stage = TMP_DIR / work_id
    raw = TMP_DIR / f"{work_id}.upload"
    try:
        sha = await _stream_to(file, raw)
        if kind == "html":
            stage.mkdir(parents=True, exist_ok=True)
            shutil.move(str(raw), str(stage / "index.html"))
        else:
            _extract_zip_safe(raw, stage)
            raw.unlink(missing_ok=True)
            _resolve_doc_html(stage)

        # 원자적 게시: stage → dest_dir (같은 파일시스템 rename)
        dest_dir.parent.mkdir(parents=True, exist_ok=True)
        if dest_dir.exists():  # overwrite 경로
            trash = TMP_DIR / f"{work_id}.old"
            os.replace(dest_dir, trash)
            try:
                os.replace(stage, dest_dir)
            finally:
                shutil.rmtree(trash, ignore_errors=True)
        else:
            os.replace(stage, dest_dir)
    except HTTPException:
        shutil.rmtree(stage, ignore_errors=True)
        raw.unlink(missing_ok=True)
        raise
    except Exception as e:  # noqa: BLE001 — 예기치 못한 오류도 안전 정리 후 500
        shutil.rmtree(stage, ignore_errors=True)
        raw.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"업로드 처리 실패: {e}")

    rel_dir = dest_dir.relative_to(UPLOADS_DIR).as_posix()          # docs/<type>/<name>_v<ver>
    rel_from_base = dest_dir.relative_to(BASE_DIR).as_posix()        # uploads/docs/...
    _enqueue_render(rel_dir)
    _audit(client_ip, uploader, doc_type, name, version, sha, rel_from_base)

    from urllib.parse import quote
    return {
        "status": "published",
        "href": "/" + quote(rel_from_base + "/index.html"),
        "pdf_pending": True,
    }
