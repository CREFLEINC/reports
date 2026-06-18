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
    GET  /<경로>      문서·에셋 파일 제공 (proposals/ + uploads/docs/ 범위로만 제한)
/healthz 외 읽기는 verify(JWT 쿠키 또는 Basic 헤더), 쓰기(/upload)는 require_uploader(uploader 역할).
브라우저는 /login 으로 로그인해 JWT 쿠키를 받고 /logout 으로 비운다. Basic 헤더는 자동화(register_report.sh)용 폴백으로 유지된다.

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
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response

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


def _identify(request: Request) -> tuple[str, str] | None:
    """(user, role) 또는 None. JWT 쿠키 우선, 그다음 Basic 헤더(자동화 폴백)."""
    token = request.cookies.get(COOKIE_NAME)
    if token:
        payload = _decode_token(token)
        if payload and payload.get("role") in ("reader", "uploader"):
            return str(payload.get("sub", "")), payload["role"]
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


def verify(request: Request) -> str:
    """읽기 인증: JWT 쿠키 또는 Basic 헤더. 미인증 브라우저는 /login 리다이렉트."""
    ident = _identify(request)
    if ident:
        return ident[0]
    if _wants_html(request):
        raise NeedsLogin(request.url.path)
    raise HTTPException(status_code=401, detail="인증이 필요합니다.")


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
"""

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


def render_index(docs: list, user: str) -> str:
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
            cards.append(
                f"""          <li class="card">
            <a class="card-main" href="{href}">
              <span class="card-title">{title}</span>
              <span class="card-meta">{rel} · {date} · {size_kb} KB</span>
            </a>{pdf_link}
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
  </div>
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
    # 전환기 보정: 구 Basic Auth 캐시를 잘못된 자격증명으로 덮어써 비운다(로그아웃 직후에만).
    poison = (
        "<script>(function(){try{var x=new XMLHttpRequest();"
        "x.open('GET','/',true,'logout','logout');x.send();}catch(e){}})();</script>"
        if loggedout else ""
    )
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
  {poison}
</body>
</html>"""


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
def index(user: str = Depends(verify)) -> HTMLResponse:
    return HTMLResponse(render_index(discover_documents(), user))


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


@app.get("/{full_path:path}")
def serve_file(full_path: str, _: str = Depends(verify)) -> FileResponse:
    candidate = (BASE_DIR / full_path).resolve()
    if not any(_is_within(candidate, r) for r in DOCS_ROOTS) or not candidate.is_file():
        raise HTTPException(status_code=404, detail="찾을 수 없습니다.")
    headers = {"X-Content-Type-Options": "nosniff"}
    # 업로드된 활성 콘텐츠(HTML)엔 엄격 CSP — 업로드 JS 의 타 문서 fetch·웜·유출 차단.
    if _is_within(candidate, UPLOADS_DOCS) and candidate.suffix.lower() in (".html", ".htm"):
        headers["Content-Security-Policy"] = (
            "connect-src 'none'; form-action 'none'; base-uri 'none'; object-src 'none'"
        )
    return FileResponse(candidate, media_type=MEDIA_TYPES.get(candidate.suffix.lower()), headers=headers)


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT)
