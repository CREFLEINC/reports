"""
CREFLE Reports — 자체 HTML 문서 열람 + 웹 업로드 서버 (FastAPI)

두 개의 문서 루트를 목차(TOC)와 함께 제공한다:
  - proposals/        git 큐레이션 문서(읽기전용 마운트)
  - uploads/docs/     웹 업로드 문서(서버 볼륨 전용 소스 오브 트루스)

라우트
    GET  /healthz     무인증 헬스체크 (도커 HEALTHCHECK 용)
    GET  /            보관 문서 목차 페이지 (요청마다 두 루트를 스캔해 동적 생성)
    GET  /upload      업로드 폼 (쓰기 자격증명 필요)
    POST /upload      업로드 처리: 검증 → 원자적 게시 → 렌더 작업 enqueue
    GET  /login       로그인 폼 (무인증)
    POST /login       자격증명 검증 → JWT 쿠키 발급
    POST /logout      JWT 쿠키 삭제(로그아웃)
    POST /api/share          자료별 외부 공개 링크 생성 (uploader 전용)
    GET  /api/share?doc=…    자료의 활성 공개 1건 조회 (uploader 전용, 모달용)
    DELETE /api/share/{tok}  공개 해제 (uploader 전용)
    GET  /s/{tok}            공개 랜딩(무인증): 열람·PDF 버튼 / 비번 폼 / 만료·해제 안내
    POST /s/{tok}/unlock     비번 검증 → 잠금해제 쿠키(무인증)
    GET  /s/{tok}/view[/…]   공개 문서·공유 에셋 제공(무인증, 형제 문서·소유외 PDF 차단)
    GET  /s/{tok}/pdf        공개 PDF 다운로드(무인증)
    GET  /<경로>      문서·에셋 파일 제공 (proposals/ + uploads/docs/ 범위로만 제한)
/healthz·/s/* 외 읽기는 verify(JWT 쿠키 또는 Basic 헤더), 쓰기(/upload)·공개관리(/api/share*)는
require_uploader(uploader 역할). /s/* 공개 라우트는 무인증이며 토큰·비번·만료로만 접근을 제한한다.
브라우저는 /login 으로 로그인해 JWT 쿠키를 받고 /logout 으로 비운다. Basic 헤더는 자동화(register_report.sh
등 Sec-Fetch-* 없는 클라이언트)용 폴백이며, 브라우저 요청에선 무시된다(캐시된 Basic 이 로그아웃을 무력화 못 하게).

환경변수 (괄호는 기본값)
    REPORTS_USER / REPORTS_PASS            읽기 Basic Auth (crefle/crefle)
    REPORTS_UPLOAD_USER / REPORTS_UPLOAD_PASS  쓰기 자격증명 (crefle / "" → 미설정 시 업로드 503)
    HOST / PORT                            바인딩 (0.0.0.0 / 8000)
    REPORTS_DOCS_DIR                       git 문서 루트 (proposals)
    REPORTS_UPLOADS_DIR                    업로드 루트 (uploads)
    REPORTS_MAX_UPLOAD_MB                  업로드 최대 크기 MB (50)
    REPORTS_SECRET_KEY                     JWT 서명 키 (미설정 시 임시 키 + 경고)
    REPORTS_TOKEN_TTL                      토큰 수명 초 (1209600=14일)
    REPORTS_COOKIE_SECURE                  TLS 뒤 1, 평문 HTTP 0 (기본 0)
    REPORTS_PUBLIC_BASE_URL                공개 링크 베이스 URL (미설정 시 요청 Origin)
    REPORTS_SHARE_UNLOCK_TTL               비번 보호 공개의 잠금해제 쿠키 수명 초 (43200=12시간)
    REPORTS_SHARES_FILE                    공개 레코드 저장 파일 (기본 uploads/shares.json)
"""
from __future__ import annotations

import base64
import html
import logging
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import jwt
import uvicorn
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel

import shares
import uploads_handler

logger = logging.getLogger("uvicorn.error")

# ──────────────────────────────────────────────────────────────────────────
# 설정
# ──────────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
DOCS_DIR = (BASE_DIR / os.environ.get("REPORTS_DOCS_DIR", "proposals")).resolve()
UPLOADS_DIR = (BASE_DIR / os.environ.get("REPORTS_UPLOADS_DIR", "uploads")).resolve()
UPLOADS_DOCS = UPLOADS_DIR / "docs"
# serve 허용 루트(허용목록). BASE_DIR 로 넓히지 않는다(server.py/.env/.git 재노출 방지).
DOCS_ROOTS = [DOCS_DIR, UPLOADS_DOCS]

USERNAME = os.environ.get("REPORTS_USER", "crefle")
PASSWORD = os.environ.get("REPORTS_PASS", "crefle")
_USING_DEFAULT_PASS = "REPORTS_PASS" not in os.environ

# 쓰기(업로드) 전용 자격증명 — 읽기와 분리. 미설정 시 /upload 는 503(fail-closed).
UPLOAD_USER = os.environ.get("REPORTS_UPLOAD_USER", "crefle")
UPLOAD_PASS = os.environ.get("REPORTS_UPLOAD_PASS", "")

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))

# ── 세션(무상태 JWT) 설정 ──
SECRET_KEY = os.environ.get("REPORTS_SECRET_KEY") or secrets.token_hex(32)
_USING_EPHEMERAL_KEY = "REPORTS_SECRET_KEY" not in os.environ
TOKEN_TTL = int(os.environ.get("REPORTS_TOKEN_TTL", str(14 * 24 * 3600)))  # 기본 14일(초)
COOKIE_SECURE = os.environ.get("REPORTS_COOKIE_SECURE", "0") == "1"
COOKIE_NAME = "reports_token"
JWT_ALG = "HS256"

# ── 공개(public share) 설정 ──
# 공개 링크 베이스 URL. 미설정 시 요청 Origin 사용. 리버스프록시 뒤 외부 도메인이 다르면 설정.
PUBLIC_BASE_URL = os.environ.get("REPORTS_PUBLIC_BASE_URL", "").rstrip("/")
SHARE_UNLOCK_COOKIE = "share_unlock"  # 비번 보호 공개의 잠금해제 상태(서명 JWT). Path=/s/<token> 스코프.
SHARE_UNLOCK_TTL = int(os.environ.get("REPORTS_SHARE_UNLOCK_TTL", str(12 * 3600)))  # 기본 12시간

# 폴더 경로(서버 기준 상대) → 목차에 표시할 사람이 읽는 섹션 이름
GROUP_LABELS = {
    "proposals": "제안서 · 데모",
    "proposals/ohmyfactory": "OhMyFactory (삼진엘앤디 DX)",
}

# 확장자 → Content-Type (mimetypes 가 OS별로 폰트를 못 맞히는 경우 대비)
MEDIA_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".pdf": "application/pdf",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".otf": "font/otf",
    ".ttf": "font/ttf",
}

TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


# ──────────────────────────────────────────────────────────────────────────
# 인증
# ──────────────────────────────────────────────────────────────────────────
def _eq(a: str, b: str) -> bool:
    """상수시간 문자열 비교(UTF-8)."""
    return secrets.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _role_for_credentials(username: str, password: str) -> str | None:
    """자격증명 → 역할('uploader'|'reader') 또는 None. 강한 uploader 계정을 먼저 검사."""
    if UPLOAD_PASS and _eq(username, UPLOAD_USER) and _eq(password, UPLOAD_PASS):
        return "uploader"
    if _eq(username, USERNAME) and _eq(password, PASSWORD):
        return "reader"
    return None


def _make_token(username: str, role: str) -> str:
    """HS256 서명 JWT 발급(sub/role/iat/exp)."""
    now = int(time.time())
    return jwt.encode(
        {"sub": username, "role": role, "iat": now, "exp": now + TOKEN_TTL},
        SECRET_KEY,
        algorithm=JWT_ALG,
    )


def _decode_token(token: str) -> dict | None:
    """서명·만료 검증. 실패 시 None. algorithms 고정으로 alg-confusion 방어."""
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[JWT_ALG])
    except jwt.InvalidTokenError:
        return None


def _is_browser(request: Request) -> bool:
    """브라우저 요청 여부. 모던 브라우저는 모든 요청에 Sec-Fetch-* 를 보내며(forbidden header
    라 사이트·JS가 못 지움), curl 등 자동화 클라이언트는 보내지 않는다."""
    h = request.headers
    return "sec-fetch-site" in h or "sec-fetch-mode" in h or "sec-fetch-dest" in h


def _identify(request: Request) -> tuple[str, str] | None:
    """(user, role) 또는 None. JWT 쿠키 우선, 그다음 Basic 헤더(자동화 전용 폴백)."""
    token = request.cookies.get(COOKIE_NAME)
    if token:
        payload = _decode_token(token)
        if payload and payload.get("role") in ("reader", "uploader"):
            return str(payload.get("sub", "")), payload["role"]
    # Basic 헤더는 자동화(curl 등)에서만 인정한다. 브라우저(Sec-Fetch-* 존재)에서는 무시 —
    # 구 시스템의 캐시된 Basic 자격증명이 로그아웃을 무력화하지 못하게 하기 위함.
    if _is_browser(request):
        return None
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Basic "):
        try:
            raw = base64.b64decode(auth[6:]).decode("utf-8")
        except Exception:  # noqa: BLE001 — 잘못된 base64 → 미인증
            return None
        user, sep, pwd = raw.partition(":")
        if not sep:
            return None
        role = _role_for_credentials(user, pwd)
        if role:
            return user, role
    return None


def _wants_html(request: Request) -> bool:
    return "text/html" in request.headers.get("accept", "")


def _safe_next(next_url: str) -> str:
    """동일출처 상대경로만 허용(오픈리다이렉트 차단)."""
    if next_url.startswith("/") and not next_url.startswith("//") and "\\" not in next_url:
        return next_url
    return "/"


class NeedsLogin(Exception):
    """미인증 브라우저 요청 → /login 리다이렉트 신호."""

    def __init__(self, next_url: str):
        self.next_url = next_url


def verify_identity(request: Request) -> tuple[str, str]:
    """읽기 인증 + 역할: (user, role). 미인증 브라우저는 /login 리다이렉트."""
    ident = _identify(request)
    if ident:
        return ident
    if _wants_html(request):
        raise NeedsLogin(request.url.path)
    raise HTTPException(status_code=401, detail="인증이 필요합니다.")


def verify(request: Request) -> str:
    """읽기 인증: JWT 쿠키 또는 Basic 헤더. 미인증 브라우저는 /login 리다이렉트."""
    return verify_identity(request)[0]


def require_uploader(request: Request) -> str:
    """쓰기 인증: uploader 역할 필요. UPLOAD_PASS 미설정이면 503(fail-closed)."""
    if not UPLOAD_PASS:
        raise HTTPException(status_code=503, detail="업로드 비활성화됨(REPORTS_UPLOAD_PASS 미설정).")
    ident = _identify(request)
    if ident and ident[1] == "uploader":
        return ident[0]
    if ident is None:
        if _wants_html(request):
            raise NeedsLogin(request.url.path)
        raise HTTPException(status_code=401, detail="업로드 인증이 필요합니다.")
    raise HTTPException(status_code=403, detail="업로드 권한이 없습니다.")


# ──────────────────────────────────────────────────────────────────────────
# 문서 탐색 + 목차 렌더
# ──────────────────────────────────────────────────────────────────────────
def _is_within(child: Path, parent: Path) -> bool:
    """child 가 parent 디렉터리 하위에 있는지(트래버설 방지)."""
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def extract_title(path: Path) -> str:
    """HTML 의 <title> 을 추출. 없으면 파일명(확장자 제외)으로 폴백."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            head = fh.read(8192)
    except OSError:
        return path.stem
    m = TITLE_RE.search(head)
    if m:
        title = html.unescape(re.sub(r"\s+", " ", m.group(1)).strip())
        if title:
            return title
    return path.stem


def _scan_root(root: Path, *, skip_index_html: bool, group_for) -> list:
    """한 루트 하위 *.html 을 수집. group_for(path)->그룹키. uploads 루트는
    index.html 이 곧 문서이므로 skip_index_html=False."""
    docs = []
    if not root.is_dir():
        return docs
    for path in root.rglob("*.html"):
        rel_from_root = path.relative_to(root)
        if any(part.startswith(".") for part in rel_from_root.parts):
            continue
        if skip_index_html and path.name.lower() == "index.html":
            continue
        rel = path.relative_to(BASE_DIR).as_posix()
        stat = path.stat()
        pdf_path = path.with_suffix(".pdf")  # index.html → index.pdf
        pdf = None
        if pdf_path.is_file():
            pdf = {
                "href": "/" + quote(pdf_path.relative_to(BASE_DIR).as_posix()),
                "stale": pdf_path.stat().st_mtime < stat.st_mtime,
            }
        docs.append(
            {
                "title": extract_title(path),
                "href": "/" + quote(rel),
                "rel": rel,
                "group": group_for(path),
                "mtime": stat.st_mtime,
                "size_kb": max(1, round(stat.st_size / 1024)),
                "pdf": pdf,
                "pending_pdf": pdf is None and not skip_index_html,  # 업로드 문서의 PDF 생성 대기
            }
        )
    return docs


def _uploads_group(path: Path) -> str:
    parts = path.relative_to(UPLOADS_DOCS).parts
    return "uploads/" + parts[0] if len(parts) > 1 else "uploads"


def discover_documents() -> list:
    """proposals/ + uploads/docs/ 두 루트를 스캔(요청마다 → 즉시 게시 반영)."""
    docs = _scan_root(
        DOCS_DIR,
        skip_index_html=True,
        group_for=lambda p: p.parent.relative_to(BASE_DIR).as_posix(),
    )
    docs += _scan_root(UPLOADS_DOCS, skip_index_html=False, group_for=_uploads_group)
    if not DOCS_DIR.is_dir() and not UPLOADS_DOCS.is_dir():
        logger.warning("문서 디렉터리가 없습니다: %s / %s", DOCS_DIR, UPLOADS_DOCS)
    return docs


def _group_label(g: str) -> str:
    if g in GROUP_LABELS:
        return GROUP_LABELS[g]
    if g == "uploads":
        return "업로드 (웹 등록)"
    if g.startswith("uploads/"):
        return "업로드 · " + g.split("/", 1)[1]
    return g


INDEX_CSS = """
  :root{
    --red:#C9252C; --ink:#1B1B1F; --ink-2:#3E4146; --muted:#77767F;
    --bg:#FBF8FD; --card:#FFFFFF; --line:#E3E1E5;
    --shadow:0 1px 2px rgba(0,0,0,.06),0 2px 6px rgba(0,0,0,.05);
  }
  @media (prefers-color-scheme: dark){
    :root{
      --red:#FF7A7E; --ink:#ECECEE; --ink-2:#C9CACE; --muted:#9A99A2;
      --bg:#1F2125; --card:#26282D; --line:#36383E;
      --shadow:0 1px 2px rgba(0,0,0,.30),0 2px 8px rgba(0,0,0,.25);
    }
  }
  *{box-sizing:border-box}
  body{
    font-family:"Spoqa Han Sans Neo","Noto Sans KR",-apple-system,BlinkMacSystemFont,
                "Segoe UI","Apple SD Gothic Neo",Roboto,sans-serif;
    margin:0; background:var(--bg); color:var(--ink); line-height:1.6;
    -webkit-font-smoothing:antialiased;
  }
  .wrap{max-width:860px; margin:0 auto; padding:56px 24px 80px;}
  .top{display:flex; align-items:baseline; gap:14px;
       border-bottom:2px solid var(--red); padding-bottom:18px;}
  .brand{font-size:1.6rem; font-weight:700; letter-spacing:-.02em; color:var(--ink);}
  .brand .dot{color:var(--red);}
  .count{margin-left:auto; color:var(--muted); font-size:.875rem;}
  .upload-link{text-decoration:none; font-size:.82rem; font-weight:700; color:#fff;
               background:var(--red); padding:6px 12px; border-radius:999px;}
  .upload-link:hover{filter:brightness(.93);}
  .lead{color:var(--muted); margin:16px 0 40px; font-size:.95rem;}
  .group{margin-bottom:40px;}
  .group-title{font-size:.78rem; font-weight:700; text-transform:uppercase;
               letter-spacing:.12em; color:var(--red); margin:0 0 14px;}
  .cards{list-style:none; margin:0; padding:0; display:grid; gap:12px;}
  .card{display:flex; align-items:stretch; background:var(--card);
        border:1px solid var(--line); border-radius:12px; box-shadow:var(--shadow);
        overflow:hidden; transition:transform .15s ease, border-color .15s ease;}
  .card:hover{transform:translateY(-2px); border-color:var(--red);}
  .card-main{flex:1; min-width:0; display:flex; flex-direction:column; gap:6px;
             text-decoration:none; color:inherit; padding:18px 20px;}
  .card-title{font-size:1.05rem; font-weight:600; color:var(--ink);}
  .card-meta{font-size:.8rem; color:var(--muted); font-variant-numeric:tabular-nums;
             word-break:break-all;}
  .card-pdf{display:flex; align-items:center; gap:6px; padding:0 18px; white-space:nowrap;
            text-decoration:none; color:var(--red); font-size:.82rem; font-weight:600;
            border-left:1px solid var(--line);}
  .card-pdf:hover{background:rgba(201,37,44,.08);}
  .card-pdf.stale{color:var(--muted);}
  .card-pdf.pending{color:var(--muted); cursor:default;}
  .empty{color:var(--muted);}
  footer{margin-top:48px; padding-top:18px; border-top:1px solid var(--line);
         color:var(--muted); font-size:.8rem;}
  .logout-form{display:flex; align-items:center; gap:10px; margin:0;}
  .who{color:var(--muted); font-size:.82rem;}
  .logout-btn{font:inherit; font-size:.82rem; font-weight:700; color:var(--ink-2);
              background:transparent; border:1px solid var(--line); border-radius:999px;
              padding:6px 12px; cursor:pointer;}
  .logout-btn:hover{border-color:var(--red); color:var(--red);}
  /* 자료별 공개(share) 버튼 + 모달 */
  .card-share{display:flex; align-items:center; gap:6px; padding:0 16px; white-space:nowrap;
              border:0; border-left:1px solid var(--line); background:transparent; cursor:pointer;
              color:var(--ink-2); font:inherit; font-size:.82rem; font-weight:600;}
  .card-share:hover{background:rgba(201,37,44,.08); color:var(--red);}
  .modal{position:fixed; inset:0; background:rgba(0,0,0,.5); display:none;
         align-items:center; justify-content:center; z-index:9999; padding:20px;}
  .modal.open{display:flex;}
  .modal-dialog{position:relative; background:var(--card); border:1px solid var(--line);
                border-radius:14px; box-shadow:var(--shadow); width:100%; max-width:440px;
                padding:26px 26px 22px;}
  .modal-close{position:absolute; top:10px; right:14px; background:transparent; border:0;
               font-size:1.3rem; line-height:1; cursor:pointer; color:var(--muted);}
  .modal-title{font-size:1.1rem; font-weight:700; margin:0 0 4px;}
  .modal-doc{font-size:.82rem; color:var(--muted); margin:0 0 18px; word-break:break-all;}
  .modal .field{display:grid; gap:5px; margin-bottom:14px;}
  .modal .field label{font-size:.84rem; font-weight:600; color:var(--ink);}
  .modal .field input{font:inherit; padding:9px 11px; border:1px solid var(--line);
                      border-radius:8px; background:var(--bg); color:var(--ink);}
  .modal .chk{display:flex; align-items:center; gap:8px; font-size:.86rem; color:var(--ink);
              margin-bottom:14px;}
  .modal .hint{font-size:.76rem; color:var(--muted);}
  .modal-result{font-size:.84rem; margin:8px 0 0; word-break:break-all;}
  .modal-result.err{color:var(--red);}
  .modal-link{display:flex; gap:8px; align-items:center; margin-top:10px;}
  .modal-link input{flex:1; min-width:0; font:inherit; font-size:.8rem; padding:8px 10px;
                    border:1px solid var(--line); border-radius:8px; background:var(--bg); color:var(--ink);}
  .modal-actions{display:flex; gap:8px; flex-wrap:wrap; margin-top:18px;}
  .btn{font:inherit; font-size:.84rem; font-weight:700; padding:9px 14px; border-radius:999px;
       border:1px solid var(--line); background:transparent; color:var(--ink); cursor:pointer;}
  .btn:hover{border-color:var(--red);}
  .btn.primary{background:var(--red); color:#fff; border-color:var(--red);}
  .btn.primary:hover{filter:brightness(.93);}
  .btn.danger{color:var(--red);}
  .btn.danger:hover{border-color:var(--red);}
"""

SHARE_MODAL_JS = """
(function(){
  const modal=document.getElementById('shareModal');
  if(!modal) return;
  const docTitle=document.getElementById('shareDocTitle');
  const form=document.getElementById('shareForm');
  const usePw=document.getElementById('shareUsePw');
  const pwField=document.getElementById('sharePwField');
  const pw=document.getElementById('sharePw');
  const expiry=document.getElementById('shareExpiry');
  const linkWrap=document.getElementById('shareLinkWrap');
  const urlInput=document.getElementById('shareUrl');
  const msg=document.getElementById('shareMsg');
  const copyBtn=document.getElementById('shareCopy');
  const revokeBtn=document.getElementById('shareRevoke');
  let currentDoc=null, currentToken=null;

  const fmt=d=>d.toISOString().split('T')[0];
  const addDays=n=>{const d=new Date(); d.setDate(d.getDate()+n); return fmt(d);};

  function resetForm(){
    form.style.display=''; linkWrap.style.display='none';
    usePw.checked=false; pwField.style.display='none'; pw.value='';
    expiry.min=addDays(0); expiry.max=addDays(365); expiry.value=addDays(30);
    msg.textContent=''; msg.className='modal-result';
  }
  function showLink(data){
    form.style.display='none'; linkWrap.style.display='';
    urlInput.value=data.share_url; currentToken=data.token;
    const lock=data.has_password?'🔒 비밀번호 보호 · ':'';
    msg.textContent='✅ '+lock+'만료일 '+data.expiry_date; msg.className='modal-result';
  }
  function showError(t){ msg.textContent='❌ '+t; msg.className='modal-result err';
    form.style.display=''; linkWrap.style.display='none'; }

  async function openModal(rel,title){
    currentDoc=rel; currentToken=null;
    docTitle.textContent=title+' · '+rel;
    resetForm(); modal.classList.add('open');
    try{
      const res=await fetch('/api/share?doc='+encodeURIComponent(rel));
      if(res.ok){const data=await res.json(); if(data.active){showLink(data);}}
    }catch(_){}
  }
  const closeModal=()=>modal.classList.remove('open');

  document.querySelectorAll('.card-share').forEach(btn=>{
    btn.addEventListener('click',e=>{e.preventDefault();openModal(btn.dataset.docRel,btn.dataset.docTitle||'');});
  });
  usePw.addEventListener('change',()=>{pwField.style.display=usePw.checked?'':'none'; if(usePw.checked)pw.focus();});
  document.getElementById('shareClose').addEventListener('click',closeModal);
  document.getElementById('shareCancel').addEventListener('click',closeModal);
  document.getElementById('shareDone').addEventListener('click',closeModal);
  modal.addEventListener('click',e=>{if(e.target===modal)closeModal();});
  document.addEventListener('keydown',e=>{if(e.key==='Escape')closeModal();});

  form.addEventListener('submit',async e=>{
    e.preventDefault();
    if(usePw.checked && !pw.value){showError('비밀번호를 입력하세요.');return;}
    msg.textContent='공개 링크 생성 중…'; msg.className='modal-result';
    const payload={doc_rel:currentDoc,use_password:usePw.checked,
                   password:usePw.checked?pw.value:'',expiry_date:expiry.value};
    try{
      const res=await fetch('/api/share',{method:'POST',headers:{'Content-Type':'application/json'},
                                          body:JSON.stringify(payload)});
      let data={}; try{data=await res.json();}catch(_){}
      if(res.ok){showLink(data);} else {showError(data.detail||('오류 '+res.status));}
    }catch(err){showError(''+err);}
  });
  copyBtn.addEventListener('click',async()=>{
    try{await navigator.clipboard.writeText(urlInput.value);}
    catch(_){urlInput.select(); try{document.execCommand('copy');}catch(__){}}
    copyBtn.textContent='✓ 복사됨'; setTimeout(()=>copyBtn.textContent='복사',1800);
  });
  revokeBtn.addEventListener('click',async()=>{
    if(!currentToken)return;
    if(!confirm('이 공개 링크를 해제할까요? 외부 접근이 즉시 차단됩니다.'))return;
    try{
      const res=await fetch('/api/share/'+encodeURIComponent(currentToken),{method:'DELETE'});
      if(res.status===204||res.ok){currentToken=null;resetForm();
        msg.textContent='공개가 해제되었습니다.';msg.className='modal-result';}
    }catch(_){}
  });
})();
"""

SHARE_MODAL_HTML = """
  <div id="shareModal" class="modal" aria-hidden="true">
    <div class="modal-dialog" role="dialog" aria-modal="true" aria-labelledby="shareModalTitle">
      <button type="button" class="modal-close" id="shareClose" aria-label="닫기">✕</button>
      <h2 class="modal-title" id="shareModalTitle">외부 공개 링크</h2>
      <p class="modal-doc" id="shareDocTitle"></p>
      <form id="shareForm">
        <label class="chk"><input type="checkbox" id="shareUsePw"> 비밀번호 사용</label>
        <div class="field" id="sharePwField" style="display:none;">
          <label for="sharePw">비밀번호</label>
          <input type="password" id="sharePw" autocomplete="new-password">
        </div>
        <div class="field">
          <label for="shareExpiry">공개 마감일</label>
          <input type="date" id="shareExpiry" required>
          <span class="hint">기본값: 오늘부터 30일 — 마감일을 자유롭게 변경하세요(최대 1년).</span>
        </div>
        <div class="modal-actions">
          <button type="submit" class="btn primary">공개 링크 생성</button>
          <button type="button" class="btn" id="shareCancel">닫기</button>
        </div>
      </form>
      <div id="shareLinkWrap" style="display:none;">
        <p class="modal-result" id="shareMsg"></p>
        <div class="modal-link">
          <input type="text" id="shareUrl" readonly aria-label="공개 링크">
          <button type="button" class="btn primary" id="shareCopy">복사</button>
        </div>
        <div class="modal-actions">
          <button type="button" class="btn danger" id="shareRevoke">공개 해제</button>
          <button type="button" class="btn" id="shareDone">완료</button>
        </div>
      </div>
    </div>
  </div>"""

UPLOAD_FORM_CSS = """
  form.up{display:grid; gap:16px; max-width:540px;}
  .field{display:grid; gap:6px;}
  .field label{font-size:.85rem; font-weight:600; color:var(--ink);}
  .field input,.field select{font:inherit; padding:10px 12px; border:1px solid var(--line);
     border-radius:8px; background:var(--card); color:var(--ink);}
  .hint{font-size:.78rem; color:var(--muted);}
  label.chk{font-size:.85rem; color:var(--ink); display:flex; align-items:center; gap:8px;}
  button.submit{font:inherit; font-weight:700; color:#fff; background:var(--red);
     border:0; border-radius:999px; padding:12px 22px; cursor:pointer; justify-self:start;}
  button.submit:hover{filter:brightness(.93);}
  #result{margin-top:18px; font-size:.92rem;}
  #result a{color:var(--red); font-weight:600;}
  .err{color:var(--red);}
"""

UPLOAD_FORM_JS = """
const f=document.getElementById('f'), r=document.getElementById('result');
f.addEventListener('submit', async (e)=>{
  e.preventDefault();
  r.textContent='업로드 중…';
  try{
    const res=await fetch('/upload',{method:'POST',body:new FormData(f)});
    let data={}; try{ data=await res.json(); }catch(_){}
    if(res.ok){
      r.innerHTML='✅ 게시됨: <a href="'+data.href+'">'+data.href+'</a>'
        + (data.pdf_pending?' · <span style="color:var(--muted)">PDF 생성 중…</span>':'');
      f.reset();
    }else{
      r.innerHTML='<span class="err">❌ '+(data.detail||('오류 '+res.status))+'</span>';
    }
  }catch(err){ r.innerHTML='<span class="err">❌ '+err+'</span>'; }
});
"""


def render_index(docs: list, user: str, can_share: bool = False) -> str:
    groups = {}
    for d in docs:
        groups.setdefault(d["group"], []).append(d)
    for items in groups.values():
        items.sort(key=lambda d: d["mtime"], reverse=True)
    ordered = sorted(groups.keys(), key=lambda g: (g.count("/"), g))

    sections = []
    for g in ordered:
        label = html.escape(_group_label(g))
        cards = []
        for d in groups[g]:
            title = html.escape(d["title"])
            rel = html.escape(d["rel"])
            href = d["href"]
            date = datetime.fromtimestamp(d["mtime"]).strftime("%Y-%m-%d")
            size_kb = d["size_kb"]
            pdf = d.get("pdf")
            pdf_link = ""
            if pdf:
                pdf_cls = "card-pdf stale" if pdf["stale"] else "card-pdf"
                pdf_title = "원본 수정 이후 PDF 재생성 필요" if pdf["stale"] else "PDF 다운로드"
                pdf_href = pdf["href"]
                pdf_link = (
                    f'\n            <a class="{pdf_cls}" href="{pdf_href}" download '
                    f'title="{pdf_title}">⬇ PDF</a>'
                )
            elif d.get("pending_pdf"):
                pdf_link = '\n            <span class="card-pdf pending" title="PDF 자동 생성 중">PDF 생성 중…</span>'
            share_btn = ""
            if can_share:
                # rel·title 은 위에서 html.escape(quote=True) 됨 → data-* 속성에 안전.
                share_btn = (
                    f'\n            <button type="button" class="card-share" '
                    f'data-doc-rel="{rel}" data-doc-title="{title}" '
                    f'title="외부 공개 링크 생성">🔗 공개</button>'
                )
            cards.append(
                f"""          <li class="card">
            <a class="card-main" href="{href}">
              <span class="card-title">{title}</span>
              <span class="card-meta">{rel} · {date} · {size_kb} KB</span>
            </a>{pdf_link}{share_btn}
          </li>"""
            )
        cards_html = "\n".join(cards)
        sections.append(
            f"""        <section class="group">
          <h2 class="group-title">{label}</h2>
          <ul class="cards">
{cards_html}
          </ul>
        </section>"""
        )

    body = "\n".join(sections) if sections else '        <p class="empty">표시할 문서가 없습니다.</p>'
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")
    count = len(docs)
    share_block = (SHARE_MODAL_HTML + f"\n  <script>{SHARE_MODAL_JS}</script>") if can_share else ""

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>CREFLE Reports</title>
<style>{INDEX_CSS}</style>
</head>
<body>
  <div class="wrap">
    <header class="top">
      <span class="brand">CREFLE <span class="dot">Reports</span></span>
      <span class="count">{count}건</span>
      <a class="upload-link" href="/upload">+ 업로드</a>
      <form class="logout-form" method="post" action="/logout">
        <span class="who">{html.escape(user)}</span>
        <button class="logout-btn" type="submit">로그아웃</button>
      </form>
    </header>
    <p class="lead">보관 중인 제안서·보고서 목록입니다. 항목을 선택하면 문서로 이동합니다.</p>
    <main>
{body}
    </main>
    <footer>생성 {generated} · 자동 색인</footer>
  </div>{share_block}
</body>
</html>"""


def render_upload_form() -> str:
    opts = "".join(f'<option value="{t}">{t}</option>' for t in ("proposal", "demo", "ohmyfactory"))
    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>리포트 업로드 · CREFLE Reports</title>
<style>{INDEX_CSS}{UPLOAD_FORM_CSS}</style>
</head>
<body>
  <div class="wrap">
    <header class="top">
      <span class="brand">CREFLE <span class="dot">Reports</span> · 업로드</span>
      <a class="upload-link" href="/" style="margin-left:auto;">← 목차</a>
    </header>
    <p class="lead">HTML 보고서(.html) 또는 자산 포함 묶음(.zip)을 올리면 즉시 게시되고 PDF가 자동 생성됩니다.</p>
    <form class="up" id="f">
      <div class="field"><label>문서 유형</label>
        <select name="doc_type">{opts}</select></div>
      <div class="field"><label>이름</label>
        <input name="name" required placeholder="예: 삼진엘앤디 신규제안"></div>
      <div class="field"><label>버전</label>
        <input name="version" required placeholder="예: 1 또는 0.1"></div>
      <div class="field"><label>파일 (.html 또는 .zip)</label>
        <input type="file" name="file" accept=".html,.htm,.zip" required>
        <span class="hint">자산(이미지·CSS·폰트)이 있으면 index.html 포함 .zip 으로 업로드</span></div>
      <label class="chk"><input type="checkbox" name="overwrite" value="1"> 같은 이름·버전 덮어쓰기</label>
      <button class="submit" type="submit">업로드 · 게시</button>
    </form>
    <div id="result"></div>
  </div>
  <script>{UPLOAD_FORM_JS}</script>
</body>
</html>"""


LOGIN_CSS = """
  .login{max-width:380px; margin:10vh auto 0;}
  .login .top{justify-content:center;}
  .login form.up{margin-top:26px;}
  .login .notice{font-size:.9rem; color:var(--muted); margin:14px 0 0;}
  .login .err{margin:0 0 2px;}
"""


def render_login_form(error: str | None = None, next_url: str = "/", loggedout: bool = False) -> str:
    err_html = f'<p class="err">❌ {html.escape(error)}</p>' if error else ""
    notice = '<p class="notice">로그아웃되었습니다.</p>' if loggedout else ""
    nxt = html.escape(next_url, quote=True)
    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>로그인 · CREFLE Reports</title>
<style>{INDEX_CSS}{UPLOAD_FORM_CSS}{LOGIN_CSS}</style>
</head>
<body>
  <div class="wrap login">
    <header class="top">
      <span class="brand">CREFLE <span class="dot">Reports</span></span>
    </header>
    {notice}
    <form class="up" method="post" action="/login">
      <input type="hidden" name="next" value="{nxt}">
      {err_html}
      <div class="field"><label>아이디</label>
        <input name="username" required autofocus autocomplete="username"></div>
      <div class="field"><label>비밀번호</label>
        <input name="password" type="password" required autocomplete="current-password"></div>
      <button class="submit" type="submit">로그인</button>
    </form>
  </div>
</body>
</html>"""


# ──────────────────────────────────────────────────────────────────────────
# 공개(public share) — 헬퍼 + 렌더
# ──────────────────────────────────────────────────────────────────────────
def _file_headers(candidate: Path) -> dict:
    """파일 응답 헤더: nosniff + 업로드 활성 콘텐츠(HTML)엔 엄격 CSP(타 문서 fetch·유출 차단)."""
    headers = {"X-Content-Type-Options": "nosniff"}
    if _is_within(candidate, UPLOADS_DOCS) and candidate.suffix.lower() in (".html", ".htm"):
        headers["Content-Security-Policy"] = (
            "connect-src 'none'; form-action 'none'; base-uri 'none'; object-src 'none'"
        )
    return headers


def _share_base_url(request: Request) -> str:
    """공개 링크 베이스: 환경변수 override 우선, 없으면 요청 Origin."""
    return PUBLIC_BASE_URL or str(request.base_url).rstrip("/")


def _share_payload(rec: dict, request: Request) -> dict:
    base = _share_base_url(request)
    return {
        "token": rec["token"],
        "share_url": f"{base}/s/{rec['token']}",
        "expiry_epoch": rec["expiry_epoch"],
        "expiry_date": datetime.fromtimestamp(rec["expiry_epoch"]).strftime("%Y-%m-%d"),
        "has_password": rec["has_password"],
        "title": rec["title"],
        "doc_rel": rec["doc_rel"],
    }


def _make_unlock_token(share_token: str) -> str:
    """비번 보호 공개의 잠금해제 토큰(서명 JWT). _decode_token 으로 검증한다."""
    now = int(time.time())
    return jwt.encode(
        {"scope": "share", "tok": share_token, "iat": now, "exp": now + SHARE_UNLOCK_TTL},
        SECRET_KEY, algorithm=JWT_ALG,
    )


def _share_unlocked(request: Request, share_token: str) -> bool:
    raw = request.cookies.get(SHARE_UNLOCK_COOKIE)
    if not raw:
        return False
    payload = _decode_token(raw)
    return bool(payload and payload.get("scope") == "share" and payload.get("tok") == share_token)


SHARE_CSS = """
  .share{max-width:560px; margin:8vh auto 0;}
  .share .top{justify-content:center;}
  .share-card{background:var(--card); border:1px solid var(--line); border-radius:14px;
              box-shadow:var(--shadow); padding:30px 28px; margin-top:26px;}
  .share-title{font-size:1.25rem; font-weight:700; margin:0 0 6px; color:var(--ink);}
  .share-sub{color:var(--muted); font-size:.86rem; margin:0 0 22px; line-height:1.5;}
  .share-actions{display:flex; flex-wrap:wrap; gap:12px;}
  .share-btn{flex:1; min-width:160px; text-align:center; text-decoration:none; font-weight:700;
             font-size:.95rem; padding:14px 18px; border-radius:999px; border:1px solid var(--line);
             color:var(--ink); background:transparent;}
  .share-btn.primary{background:var(--red); color:#fff; border-color:var(--red);}
  .share-btn.primary:hover{filter:brightness(.93);}
  .share-btn.ghost:hover{border-color:var(--red); color:var(--red);}
  .share-meta{margin-top:22px; padding-top:16px; border-top:1px solid var(--line);
              color:var(--muted); font-size:.8rem;}
  .share .field{margin-top:18px;}
"""


def _share_chrome(title: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{html.escape(title)} · CREFLE Reports</title>
<style>{INDEX_CSS}{UPLOAD_FORM_CSS}{LOGIN_CSS}{SHARE_CSS}</style>
</head>
<body>
  <div class="wrap share">
    <header class="top">
      <span class="brand">CREFLE <span class="dot">Reports</span></span>
    </header>
{body}
  </div>
</body>
</html>"""


def render_share_landing(rec: dict, *, pdf_available: bool) -> str:
    title = html.escape(rec["title"])
    token = rec["token"]  # token_urlsafe → [A-Za-z0-9_-], URL·HTML 안전
    expiry = datetime.fromtimestamp(rec["expiry_epoch"]).strftime("%Y-%m-%d")
    lock = "🔒 비밀번호 보호 · " if rec["has_password"] else ""
    pdf_btn = (
        f'\n        <a class="share-btn ghost" href="/s/{token}/pdf">⬇ PDF 다운로드</a>'
        if pdf_available else ""
    )
    body = f"""    <div class="share-card">
      <h1 class="share-title">{title}</h1>
      <p class="share-sub">CREFLE 가 공개한 자료입니다. 아래에서 문서를 열람하거나 PDF 를 내려받을 수 있습니다.</p>
      <div class="share-actions">
        <a class="share-btn primary" href="/s/{token}/view/">📄 문서 열람</a>{pdf_btn}
      </div>
      <p class="share-meta">{lock}공개 만료일 {expiry}</p>
    </div>"""
    return _share_chrome(rec["title"], body)


def render_share_password(token: str, error: str | None = None) -> str:
    err = f'<p class="err">❌ {html.escape(error)}</p>' if error else ""
    body = f"""    <div class="share-card">
      <h1 class="share-title">🔒 비밀번호 입력</h1>
      <p class="share-sub">이 자료는 비밀번호로 보호되어 있습니다.</p>
      {err}
      <form class="up" method="post" action="/s/{html.escape(token, quote=True)}/unlock">
        <div class="field"><label>비밀번호</label>
          <input name="password" type="password" required autofocus autocomplete="off"></div>
        <button class="submit" type="submit">열람</button>
      </form>
    </div>"""
    return _share_chrome("비밀번호 입력", body)


def render_share_gone() -> str:
    body = """    <div class="share-card">
      <h1 class="share-title">링크를 찾을 수 없습니다</h1>
      <p class="share-sub">만료되었거나 해제된 공개 링크입니다. 자료 제공자에게 문의해 주세요.</p>
    </div>"""
    return _share_chrome("공개 링크", body)


# ──────────────────────────────────────────────────────────────────────────
# 앱
# ──────────────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    if _USING_DEFAULT_PASS:
        logger.warning("⚠️  REPORTS_PASS 가 기본값(crefle)입니다. 운영 전 강한 비밀번호를 설정하세요.")
    if _USING_EPHEMERAL_KEY:
        logger.warning("⚠️  REPORTS_SECRET_KEY 미설정 → 임시 키 사용(재시작 시 모든 로그인 무효화). 운영 전 설정하세요.")
    if not UPLOAD_PASS:
        logger.warning("⚠️  REPORTS_UPLOAD_PASS 미설정 → 업로드(/upload)는 503 으로 비활성화됩니다.")
    logger.info("CREFLE Reports · proposals=%s · uploads=%s · http://%s:%s", DOCS_DIR, UPLOADS_DOCS, HOST, PORT)
    yield


app = FastAPI(title="CREFLE Reports", lifespan=lifespan)


@app.exception_handler(NeedsLogin)
async def _needs_login_handler(request: Request, exc: NeedsLogin) -> RedirectResponse:
    dest = "/login"
    nxt = _safe_next(exc.next_url)
    if nxt != "/":
        dest += "?next=" + quote(nxt, safe="")
    return RedirectResponse(dest, status_code=303)


@app.get("/healthz")
def healthz() -> dict:
    """무인증 헬스체크(도커 HEALTHCHECK 용)."""
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def index(ident: tuple = Depends(verify_identity)) -> HTMLResponse:
    user, role = ident
    return HTMLResponse(render_index(discover_documents(), user, can_share=(role == "uploader")))


# 업로드 라우트는 catch-all 보다 먼저 등록해야 한다(그렇지 않으면 catch-all 이 삼킴).
@app.get("/upload", response_class=HTMLResponse)
def upload_form(_: str = Depends(require_uploader)) -> HTMLResponse:
    return HTMLResponse(render_upload_form())


@app.post("/upload")
async def upload(
    request: Request,
    doc_type: str = Form(...),
    name: str = Form(...),
    version: str = Form(...),
    file: UploadFile = File(...),
    overwrite: int = Form(0),
    uploader: str = Depends(require_uploader),
) -> JSONResponse:
    client_ip = request.client.host if request.client else "?"
    result = await uploads_handler.handle_upload(
        file=file,
        doc_type=doc_type,
        name=name,
        version=version,
        client_ip=client_ip,
        uploader=uploader,
        overwrite=bool(overwrite),
    )
    return JSONResponse(result, status_code=201)


@app.get("/login")
def login_form(request: Request, next: str = "/", loggedout: int = 0) -> Response:
    if _identify(request):
        return RedirectResponse(_safe_next(next), status_code=303)
    return HTMLResponse(render_login_form(next_url=_safe_next(next), loggedout=bool(loggedout)))


@app.post("/login")
def login_submit(username: str = Form(...), password: str = Form(...), next: str = Form("/")) -> Response:
    target = _safe_next(next)
    role = _role_for_credentials(username, password)
    if not role:
        return HTMLResponse(
            render_login_form(error="아이디 또는 비밀번호가 올바르지 않습니다.", next_url=target),
            status_code=401,
        )
    resp = RedirectResponse(target, status_code=303)
    resp.set_cookie(
        COOKIE_NAME, _make_token(username, role),
        max_age=TOKEN_TTL, httponly=True, samesite="lax", secure=COOKIE_SECURE, path="/",
    )
    return resp


@app.post("/logout")
def logout() -> Response:
    resp = RedirectResponse("/login?loggedout=1", status_code=303)
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp


# ──────────────────────────────────────────────────────────────────────────
# 자료별 공개(public share) — catch-all 보다 먼저 등록해야 한다.
# 관리 API(/api/share*)는 uploader 전용. 공개 접근(/s/*)은 무인증(토큰·비번으로만 제한).
# ──────────────────────────────────────────────────────────────────────────
class ShareCreateRequest(BaseModel):
    doc_rel: str
    use_password: bool = False
    password: str = ""           # Python 3.9 + pydantic v2: Optional 대신 빈 문자열 기본값
    expiry_date: str             # 'YYYY-MM-DD' (마감일)


def _find_doc(doc_rel: str) -> dict | None:
    """discover_documents() 화이트리스트에서 rel 일치 문서(임의 파일/server.py 공개 차단)."""
    for d in discover_documents():
        if d["rel"] == doc_rel:
            return d
    return None


@app.post("/api/share")
def api_share_create(req: ShareCreateRequest, request: Request,
                     uploader: str = Depends(require_uploader)) -> JSONResponse:
    doc = _find_doc(req.doc_rel)
    candidate = (BASE_DIR / req.doc_rel).resolve()
    if not doc or not any(_is_within(candidate, r) for r in DOCS_ROOTS) or not candidate.is_file():
        raise HTTPException(status_code=404, detail="문서를 찾을 수 없습니다.")
    password = req.password if req.use_password else None
    if req.use_password and not (password and password.strip()):
        raise HTTPException(status_code=422, detail="비밀번호를 입력하세요.")
    try:
        expiry = shares.compute_expiry(req.expiry_date)
        shares.validate_expiry(expiry)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    doc_dir = candidate.parent.relative_to(BASE_DIR).as_posix()
    rec = shares.create_share(doc_rel=req.doc_rel, doc_dir=doc_dir, title=doc["title"],
                              password=password, expiry_epoch=expiry, created_by=uploader)
    return JSONResponse(_share_payload(rec, request), status_code=201)


@app.get("/api/share")
def api_share_current(request: Request, doc: str = Query(...),
                      _: str = Depends(require_uploader)) -> JSONResponse:
    """해당 자료의 활성 공개 1건(모달 재오픈 시 기존 링크/해제 노출용)."""
    rec = shares.find_active_by_doc(doc)
    if not rec:
        return JSONResponse({"active": False})
    payload = _share_payload(rec, request)
    payload["active"] = True
    return JSONResponse(payload)


@app.delete("/api/share/{token}")
def api_share_delete(token: str, _: str = Depends(require_uploader)) -> Response:
    shares.delete_share(token)  # 없는 토큰도 멱등 — 204
    return Response(status_code=204)


@app.get("/s/{token}")
def share_landing(token: str, request: Request) -> Response:
    rec = shares.get_share(token)
    if not rec:
        return HTMLResponse(render_share_gone(), status_code=404)
    if rec["has_password"] and not _share_unlocked(request, token):
        return HTMLResponse(render_share_password(token))
    pdf_available = (BASE_DIR / rec["doc_rel"]).resolve().with_suffix(".pdf").is_file()
    return HTMLResponse(render_share_landing(rec, pdf_available=pdf_available))


@app.post("/s/{token}/unlock")
def share_unlock(token: str, password: str = Form(...)) -> Response:
    rec = shares.get_share(token)
    if not rec:
        return HTMLResponse(render_share_gone(), status_code=404)
    if not rec["has_password"]:
        return RedirectResponse(f"/s/{token}", status_code=303)
    if not shares.verify_password(password, rec.get("pw_salt") or "", rec.get("pw_hash") or ""):
        return HTMLResponse(render_share_password(token, error="비밀번호가 올바르지 않습니다."),
                            status_code=401)
    resp = RedirectResponse(f"/s/{token}", status_code=303)
    resp.set_cookie(SHARE_UNLOCK_COOKIE, _make_unlock_token(token), max_age=SHARE_UNLOCK_TTL,
                    httponly=True, samesite="lax", secure=COOKIE_SECURE, path=f"/s/{token}")
    return resp


@app.get("/s/{token}/view")
def share_view_redirect(token: str) -> RedirectResponse:
    # 트레일링 슬래시 → 문서의 상대 에셋(assets/…, *.css)이 /s/<token>/view/ 기준으로 해결된다.
    return RedirectResponse(f"/s/{token}/view/", status_code=307)


@app.get("/s/{token}/view/{subpath:path}")
def share_view(token: str, request: Request, subpath: str = "") -> Response:
    rec = shares.get_share(token)
    if not rec:
        return HTMLResponse(render_share_gone(), status_code=404)
    if rec["has_password"] and not _share_unlocked(request, token):
        return RedirectResponse(f"/s/{token}", status_code=303)
    doc_dir = (BASE_DIR / rec["doc_dir"]).resolve()
    own_html = (BASE_DIR / rec["doc_rel"]).resolve()
    target = own_html if not subpath else (doc_dir / subpath).resolve()
    if (not _is_within(target, doc_dir)
            or not any(_is_within(target, r) for r in DOCS_ROOTS)
            or not target.is_file()):
        raise HTTPException(status_code=404, detail="찾을 수 없습니다.")
    # 형제 문서 과다노출 차단: 소유 html 외의 .html/.htm·.pdf 거부(공유 에셋만 허용).
    if target != own_html and target.suffix.lower() in (".html", ".htm", ".pdf"):
        raise HTTPException(status_code=404, detail="찾을 수 없습니다.")
    return FileResponse(target, media_type=MEDIA_TYPES.get(target.suffix.lower()),
                        headers=_file_headers(target))


@app.get("/s/{token}/pdf")
def share_pdf(token: str, request: Request) -> Response:
    rec = shares.get_share(token)
    if not rec:
        return HTMLResponse(render_share_gone(), status_code=404)
    if rec["has_password"] and not _share_unlocked(request, token):
        return RedirectResponse(f"/s/{token}", status_code=303)
    pdf = (BASE_DIR / rec["doc_rel"]).resolve().with_suffix(".pdf")
    if not pdf.is_file() or not any(_is_within(pdf, r) for r in DOCS_ROOTS):
        raise HTTPException(status_code=404, detail="PDF 가 아직 준비되지 않았습니다.")
    return FileResponse(pdf, media_type="application/pdf", filename=pdf.name,
                        headers={"X-Content-Type-Options": "nosniff"})


@app.get("/{full_path:path}")
def serve_file(full_path: str, _: str = Depends(verify)) -> FileResponse:
    candidate = (BASE_DIR / full_path).resolve()
    if not any(_is_within(candidate, r) for r in DOCS_ROOTS) or not candidate.is_file():
        raise HTTPException(status_code=404, detail="찾을 수 없습니다.")
    return FileResponse(candidate, media_type=MEDIA_TYPES.get(candidate.suffix.lower()),
                        headers=_file_headers(candidate))


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT)
