"""
CREFLE Reports — 자체 HTML 문서 열람 서버 (FastAPI)

proposals/ 하위에 보관된 HTML 보고서를, 자동 생성되는 목차(TOC)와 함께 제공한다.

라우트
    GET /            보관 문서 목차 페이지 (요청마다 폴더를 스캔해 동적 생성)
    GET /<경로>      문서·에셋 파일 제공 (proposals/ 범위로만 제한)
모든 경로는 HTTP Basic Auth 로 보호된다.

실행
    pip install -r requirements.txt
    python3 server.py                 # 0.0.0.0:8000

환경변수 (모두 선택, 괄호는 기본값)
    REPORTS_USER       Basic Auth 사용자명          (crefle)
    REPORTS_PASS       Basic Auth 비밀번호          (crefle — 운영 시 반드시 변경)
    HOST               바인딩 주소                  (0.0.0.0)
    PORT               포트                         (8000)
    REPORTS_DOCS_DIR   문서 루트(서버 위치 기준 상대) (proposals)
"""
from __future__ import annotations

import html
import logging
import os
import re
import secrets
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

logger = logging.getLogger("uvicorn.error")

# ──────────────────────────────────────────────────────────────────────────
# 설정
# ──────────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
DOCS_DIR = (BASE_DIR / os.environ.get("REPORTS_DOCS_DIR", "proposals")).resolve()

USERNAME = os.environ.get("REPORTS_USER", "crefle")
PASSWORD = os.environ.get("REPORTS_PASS", "crefle")
_USING_DEFAULT_PASS = "REPORTS_PASS" not in os.environ

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))

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
# 인증 (모든 라우트에 전역 적용)
# ──────────────────────────────────────────────────────────────────────────
security = HTTPBasic()


def verify(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    ok_user = secrets.compare_digest(
        credentials.username.encode("utf-8"), USERNAME.encode("utf-8")
    )
    ok_pass = secrets.compare_digest(
        credentials.password.encode("utf-8"), PASSWORD.encode("utf-8")
    )
    if not (ok_user and ok_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="인증이 필요합니다.",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


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
            head = fh.read(8192)  # title 은 항상 <head> 안 → 앞부분만 읽음
    except OSError:
        return path.stem
    m = TITLE_RE.search(head)
    if m:
        title = html.unescape(re.sub(r"\s+", " ", m.group(1)).strip())
        if title:
            return title
    return path.stem


def discover_documents() -> list:
    """DOCS_DIR 하위의 *.html 보고서를 수집한다(숨김 경로·index.html 제외)."""
    docs = []
    if not DOCS_DIR.is_dir():
        logger.warning("문서 디렉터리를 찾을 수 없습니다: %s", DOCS_DIR)
        return docs
    for path in DOCS_DIR.rglob("*.html"):
        rel_from_docs = path.relative_to(DOCS_DIR)
        if any(part.startswith(".") for part in rel_from_docs.parts):
            continue  # 숨김 폴더/파일
        if path.name.lower() == "index.html":
            continue  # 폴더 랜딩 페이지는 목록에서 제외
        rel = path.relative_to(BASE_DIR).as_posix()
        group = path.parent.relative_to(BASE_DIR).as_posix()
        stat = path.stat()
        docs.append(
            {
                "title": extract_title(path),
                "href": "/" + quote(rel),
                "rel": rel,
                "group": group,
                "mtime": stat.st_mtime,
                "size_kb": max(1, round(stat.st_size / 1024)),
            }
        )
    return docs


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
  .lead{color:var(--muted); margin:16px 0 40px; font-size:.95rem;}
  .group{margin-bottom:40px;}
  .group-title{font-size:.78rem; font-weight:700; text-transform:uppercase;
               letter-spacing:.12em; color:var(--red); margin:0 0 14px;}
  .cards{list-style:none; margin:0; padding:0; display:grid; gap:12px;}
  .card a{display:flex; flex-direction:column; gap:6px; text-decoration:none;
          color:inherit; background:var(--card); border:1px solid var(--line);
          border-radius:12px; padding:18px 20px; box-shadow:var(--shadow);
          transition:transform .15s ease, border-color .15s ease;}
  .card a:hover{transform:translateY(-2px); border-color:var(--red);}
  .card-title{font-size:1.05rem; font-weight:600; color:var(--ink);}
  .card-meta{font-size:.8rem; color:var(--muted); font-variant-numeric:tabular-nums;
             word-break:break-all;}
  .empty{color:var(--muted);}
  footer{margin-top:48px; padding-top:18px; border-top:1px solid var(--line);
         color:var(--muted); font-size:.8rem;}
"""


def render_index(docs: list) -> str:
    groups = {}
    for d in docs:
        groups.setdefault(d["group"], []).append(d)
    for items in groups.values():
        items.sort(key=lambda d: d["mtime"], reverse=True)  # 그룹 내 최신순
    ordered = sorted(groups.keys(), key=lambda g: (g.count("/"), g))  # 상위 폴더 먼저

    sections = []
    for g in ordered:
        label = html.escape(GROUP_LABELS.get(g, g))
        cards = []
        for d in groups[g]:
            title = html.escape(d["title"])
            rel = html.escape(d["rel"])
            href = d["href"]
            date = datetime.fromtimestamp(d["mtime"]).strftime("%Y-%m-%d")
            size_kb = d["size_kb"]
            cards.append(
                f"""          <li class="card">
            <a href="{href}">
              <span class="card-title">{title}</span>
              <span class="card-meta">{rel} · {date} · {size_kb} KB</span>
            </a>
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
    </header>
    <p class="lead">보관 중인 제안서·보고서 목록입니다. 항목을 선택하면 문서로 이동합니다.</p>
    <main>
{body}
    </main>
    <footer>생성 {generated} · 자동 색인</footer>
  </div>
</body>
</html>"""


# ──────────────────────────────────────────────────────────────────────────
# 앱
# ──────────────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    if _USING_DEFAULT_PASS:
        logger.warning(
            "⚠️  REPORTS_PASS 가 기본값(crefle)입니다. 운영 전 환경변수로 강한 비밀번호를 설정하세요."
        )
    logger.info("CREFLE Reports · 문서 루트=%s · http://%s:%s", DOCS_DIR, HOST, PORT)
    yield


app = FastAPI(title="CREFLE Reports", dependencies=[Depends(verify)], lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse(render_index(discover_documents()))


@app.get("/{full_path:path}")
def serve_file(full_path: str) -> FileResponse:
    candidate = (BASE_DIR / full_path).resolve()
    if not _is_within(candidate, DOCS_DIR) or not candidate.is_file():
        raise HTTPException(status_code=404, detail="찾을 수 없습니다.")
    return FileResponse(candidate, media_type=MEDIA_TYPES.get(candidate.suffix.lower()))


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT)
