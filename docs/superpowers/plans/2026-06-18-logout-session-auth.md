# 로그아웃(JWT 무상태 인증) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** CREFLE Reports에 쿠키에 담은 무상태 JWT 로그인/로그아웃을 추가하고, 기존 Basic Auth는 자동화용 폴백으로 유지한다.

**Architecture:** `server.py`의 인증 의존성(`verify`/`require_uploader`)을 "JWT 쿠키 **또는** Basic 헤더" 하이브리드로 교체한다. 사람 브라우저는 `/login` 폼으로 HS256 서명 JWT 쿠키를 발급받고(`exp` 만료), `/logout`이 그 쿠키를 삭제한다. 미인증 브라우저는 `WWW-Authenticate: Basic` 대신 `/login`으로 303 리다이렉트하므로 브라우저가 Basic을 캐시하지 않아 로그아웃이 확실히 동작한다. 자동화(`register_report.sh`의 `curl -u`)·렌더러·healthcheck는 영향 없음.

**Tech Stack:** Python · FastAPI/Starlette · PyJWT(HS256) · pytest + httpx(TestClient)

**Spec:** `docs/superpowers/specs/2026-06-18-logout-session-auth-design.md`

---

## File Structure

- **Modify** `server.py` — 인증 코어(JWT/자격증명 헬퍼, `_identify`, 의존성), `/login`·`/logout` 라우트, 로그인 폼 렌더, 인덱스 헤더 로그아웃 버튼, 설정/임포트, lifespan 경고, docstring.
- **Modify** `requirements.txt` — `pyjwt>=2.0` 추가.
- **Create** `requirements-dev.txt` — `pytest`, `httpx`(TestClient 의존성).
- **Create** `conftest.py`(repo 루트, 비어 있음) — pytest가 repo 루트를 import 경로에 넣도록.
- **Create** `tests/test_auth.py` — 인증/로그인/로그아웃/회귀 테스트.
- **Modify** `.env.example` — JWT 신규 env 3개.
- **Modify** `docker-compose.yml` — 신규 env 전달 + 이미지 태그 `1.2 → 1.3`.
- **Modify** `README.md` — 로그인/로그아웃·신규 env·"Basic은 자동화용" 명시.

> 모든 작업은 이미 체크아웃된 브랜치 `feature/logout-session-auth`에서 진행한다.

---

## Task 1: 의존성 + 테스트 하네스 구축

**Files:**
- Modify: `requirements.txt`
- Create: `requirements-dev.txt`
- Create: `conftest.py`
- Create: `tests/test_auth.py`

- [ ] **Step 1: `pyjwt`를 런타임 의존성에 추가**

`requirements.txt`를 다음으로 만든다(기존 3줄 + 1줄):

```
fastapi>=0.110
uvicorn[standard]>=0.27
python-multipart>=0.0.7
pyjwt>=2.0
```

- [ ] **Step 2: dev 의존성 파일 생성**

`requirements-dev.txt`:

```
pytest>=8.0
httpx>=0.27
```

- [ ] **Step 3: repo 루트 conftest 생성(import 경로 보장)**

`conftest.py` (빈 파일이면 충분 — pytest가 이 파일이 있는 디렉터리를 sys.path에 prepend):

```python
# pytest가 repo 루트를 import 경로에 추가하도록 하는 마커. (server 모듈 import용)
```

- [ ] **Step 4: 의존성 설치**

Run:
```bash
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
```
Expected: `pyjwt`, `httpx`, `pytest` 설치 완료(이미 있으면 "already satisfied").

- [ ] **Step 5: 테스트 하네스 스모크 테스트 작성**

`tests/test_auth.py` (env는 `import server` **이전**에 설정해야 함 — 설정이 import 시점에 읽힘):

```python
import os
import time

# server import 전에 결정적 자격증명/키를 강제 설정한다.
os.environ["REPORTS_USER"] = "reader"
os.environ["REPORTS_PASS"] = "readerpass"
os.environ["REPORTS_UPLOAD_USER"] = "uploader"
os.environ["REPORTS_UPLOAD_PASS"] = "uploaderpass"
os.environ["REPORTS_SECRET_KEY"] = "test-secret-deadbeef-0123456789abcdef"

import jwt
import pytest
from fastapi.testclient import TestClient

import server
from server import app

client = TestClient(app)


def test_healthz_no_auth():
    assert client.get("/healthz").status_code == 200
```

- [ ] **Step 6: 스모크 테스트 실행(통과 확인)**

Run: `.venv/bin/python -m pytest tests/test_auth.py -v`
Expected: `test_healthz_no_auth PASSED` (1 passed). `python -m pytest`로 실행해 repo 루트가 sys.path에 들어가야 `import server`가 된다.

- [ ] **Step 7: 커밋**

```bash
git add requirements.txt requirements-dev.txt conftest.py tests/test_auth.py
git commit -m "test: add pyjwt dep + auth test harness (healthz smoke)"
```

---

## Task 2: JWT + 자격증명 코어 헬퍼

**Files:**
- Modify: `server.py` (임포트, 설정 블록, 인증 섹션)
- Test: `tests/test_auth.py`

- [ ] **Step 1: 실패하는 단위 테스트 작성**

`tests/test_auth.py`의 `test_healthz_no_auth` 아래에 추가:

```python
def test_token_roundtrip():
    tok = server._make_token("alice", "reader")
    payload = server._decode_token(tok)
    assert payload is not None
    assert payload["sub"] == "alice"
    assert payload["role"] == "reader"


def test_decode_rejects_tampered():
    assert server._decode_token("not.a.jwt") is None


def test_decode_rejects_expired():
    now = int(time.time())
    tok = jwt.encode({"sub": "x", "role": "reader", "iat": now - 100, "exp": now - 10},
                     server.SECRET_KEY, algorithm="HS256")
    assert server._decode_token(tok) is None


def test_role_for_credentials():
    assert server._role_for_credentials("uploader", "uploaderpass") == "uploader"
    assert server._role_for_credentials("reader", "readerpass") == "reader"
    assert server._role_for_credentials("reader", "WRONG") is None
    assert server._role_for_credentials("nobody", "x") is None
```

- [ ] **Step 2: 실패 확인**

Run: `.venv/bin/python -m pytest tests/test_auth.py -v`
Expected: FAIL — `AttributeError: module 'server' has no attribute '_make_token'` (그리고 `SECRET_KEY` 없음).

- [ ] **Step 3: 임포트 수정**

`server.py` 상단 임포트에서 — `import base64`, `import time`, `import jwt`를 추가하고, `fastapi.responses` 임포트에 `RedirectResponse, Response`를 추가하고, **사용하지 않게 될** `from fastapi.security import HTTPBasic, HTTPBasicCredentials`를 제거한다.

변경 전:
```python
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
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

import uploads_handler
```

변경 후:
```python
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
```

- [ ] **Step 4: 설정 블록에 JWT 설정 추가**

`server.py`의 `PORT = int(os.environ.get("PORT", "8000"))` 줄 **바로 아래**에 추가:

```python

# ── 세션(무상태 JWT) 설정 ──
SECRET_KEY = os.environ.get("REPORTS_SECRET_KEY") or secrets.token_hex(32)
_USING_EPHEMERAL_KEY = "REPORTS_SECRET_KEY" not in os.environ
TOKEN_TTL = int(os.environ.get("REPORTS_TOKEN_TTL", str(14 * 24 * 3600)))  # 기본 14일(초)
COOKIE_SECURE = os.environ.get("REPORTS_COOKIE_SECURE", "0") == "1"
COOKIE_NAME = "reports_token"
JWT_ALG = "HS256"
```

- [ ] **Step 5: 인증 섹션을 헬퍼로 교체**

`server.py`의 인증 섹션 전체 — 즉 `security = HTTPBasic()`부터 `require_uploader(...)` 함수 끝까지(현재 99~128행) — 를 아래로 **교체**한다:

```python
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
```

- [ ] **Step 6: 통과 확인**

Run: `.venv/bin/python -m pytest tests/test_auth.py -v`
Expected: 위 4개 신규 테스트 + healthz = 5 passed. (이 시점에는 `verify`/`require_uploader`가 아직 없어 라우트가 깨지지만, import는 됨 — 다음 단계에서 라우트 의존성을 복구한다. import 자체가 실패하면 STOP하고 Task 3을 먼저 진행.)

> 참고: 이 단계에서 `server.py`는 `verify`/`require_uploader`를 참조하는 라우트(현재 파일 하단)가 아직 옛 정의를 잃었으므로 **import 시점에 NameError**가 날 수 있다. 그럴 경우 Step 6의 pytest가 collection 단계에서 실패한다 → **곧바로 Task 3을 이어서 수행**해 의존성을 복구한 뒤 한 번에 통과시킨다. (Task 2와 Task 3은 한 커밋으로 묶어도 좋다.)

- [ ] **Step 7: 커밋(Task 3까지 끝낸 뒤 묶어서 커밋해도 됨)**

```bash
git add server.py tests/test_auth.py
git commit -m "feat(auth): add JWT + credential core helpers"
```

---

## Task 3: 하이브리드 인증 의존성 + 미인증 리다이렉트

**Files:**
- Modify: `server.py` (인증 섹션에 의존성 추가, app 생성 후 예외 핸들러 등록)
- Test: `tests/test_auth.py`

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_auth.py`에 추가:

```python
def test_browser_unauth_redirects_to_login():
    r = client.get("/", headers={"accept": "text/html"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login")


def test_api_unauth_returns_401():
    r = client.get("/", headers={"accept": "application/json"}, follow_redirects=False)
    assert r.status_code == 401


def test_basic_reader_serves_index():
    r = client.get("/", auth=("reader", "readerpass"))
    assert r.status_code == 200


def test_jwt_cookie_grants_access():
    c = TestClient(app)
    c.cookies.set("reports_token", server._make_token("reader", "reader"))
    r = c.get("/", headers={"accept": "text/html"})
    assert r.status_code == 200


def test_tampered_cookie_redirects():
    c = TestClient(app)
    c.cookies.set("reports_token", "not.a.valid.jwt")
    r = c.get("/", headers={"accept": "text/html"}, follow_redirects=False)
    assert r.status_code == 303


def test_reader_cannot_access_upload():
    c = TestClient(app)
    c.cookies.set("reports_token", server._make_token("reader", "reader"))
    r = c.get("/upload", headers={"accept": "text/html"}, follow_redirects=False)
    assert r.status_code == 403


def test_uploader_can_access_upload():
    c = TestClient(app)
    c.cookies.set("reports_token", server._make_token("uploader", "uploader"))
    r = c.get("/upload")
    assert r.status_code == 200
```

- [ ] **Step 2: 실패 확인**

Run: `.venv/bin/python -m pytest tests/test_auth.py -v`
Expected: FAIL (의존성 미정의로 NameError 또는 라우트 오류).

- [ ] **Step 3: 의존성·헬퍼·예외 추가**

`server.py`의 `_decode_token` 함수 **아래**(여전히 인증 섹션)에 추가:

```python
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
```

- [ ] **Step 4: 예외 핸들러 등록**

`server.py`에서 `app = FastAPI(title="CREFLE Reports", lifespan=lifespan)` 줄 **바로 아래**에 추가:

```python


@app.exception_handler(NeedsLogin)
async def _needs_login_handler(request: Request, exc: NeedsLogin) -> RedirectResponse:
    dest = "/login"
    nxt = _safe_next(exc.next_url)
    if nxt != "/":
        dest += "?next=" + quote(nxt, safe="")
    return RedirectResponse(dest, status_code=303)
```

- [ ] **Step 5: 통과 확인**

Run: `.venv/bin/python -m pytest tests/test_auth.py -v`
Expected: Task 1~3 테스트 전부 PASS (12 passed).

- [ ] **Step 6: 커밋**

```bash
git add server.py tests/test_auth.py
git commit -m "feat(auth): hybrid JWT-cookie/Basic dependencies + login redirect"
```

---

## Task 4: 로그인/로그아웃 라우트 + 로그인 폼

**Files:**
- Modify: `server.py` (로그인 폼 렌더 함수, `/login`·`/logout` 라우트 — catch-all 앞)
- Test: `tests/test_auth.py`

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_auth.py`에 추가:

```python
def test_login_sets_cookie_and_grants_access():
    c = TestClient(app)
    r = c.post("/login", data={"username": "reader", "password": "readerpass", "next": "/"},
               follow_redirects=False)
    assert r.status_code == 303
    assert "reports_token=" in r.headers.get("set-cookie", "")
    r2 = c.get("/", headers={"accept": "text/html"})
    assert r2.status_code == 200


def test_login_wrong_credentials_rejected():
    c = TestClient(app)
    r = c.post("/login", data={"username": "reader", "password": "WRONG", "next": "/"},
               follow_redirects=False)
    assert r.status_code == 401
    assert "reports_token=" not in r.headers.get("set-cookie", "")


def test_login_open_redirect_blocked():
    c = TestClient(app)
    r = c.post("/login", data={"username": "reader", "password": "readerpass",
                               "next": "//evil.example.com"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/"


def test_logout_clears_cookie():
    c = TestClient(app)
    c.cookies.set("reports_token", server._make_token("reader", "reader"))
    r = c.post("/logout", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login")
    sc = r.headers.get("set-cookie", "").lower()
    assert "reports_token=" in sc and ("max-age=0" in sc or "expires=" in sc)


def test_login_page_renders():
    r = client.get("/login")
    assert r.status_code == 200
    assert "로그인" in r.text
```

- [ ] **Step 2: 실패 확인**

Run: `.venv/bin/python -m pytest tests/test_auth.py -k "login or logout" -v`
Expected: FAIL (404 — 라우트 없음).

- [ ] **Step 3: 로그인 폼 렌더 함수 추가**

`server.py`에서 `render_upload_form()` 함수 정의 **바로 아래**에 추가:

```python
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
```

- [ ] **Step 4: `/login`·`/logout` 라우트 추가(catch-all 앞)**

`server.py`에서 `@app.post("/upload")` 핸들러 정의가 끝난 직후, **`@app.get("/{full_path:path}")` 정의 바로 앞**에 추가:

```python
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
```

- [ ] **Step 5: 통과 확인**

Run: `.venv/bin/python -m pytest tests/test_auth.py -v`
Expected: 신규 5개 포함 전부 PASS.

- [ ] **Step 6: 커밋**

```bash
git add server.py tests/test_auth.py
git commit -m "feat(auth): add /login and /logout routes + login form"
```

---

## Task 5: 인덱스 헤더 로그아웃 버튼

**Files:**
- Modify: `server.py` (`INDEX_CSS` 로그아웃 스타일, `render_index` 시그니처/헤더, `index` 라우트)
- Test: `tests/test_auth.py`

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_auth.py`에 추가:

```python
def test_index_shows_user_and_logout():
    c = TestClient(app)
    c.cookies.set("reports_token", server._make_token("reader", "reader"))
    r = c.get("/", headers={"accept": "text/html"})
    assert r.status_code == 200
    assert "로그아웃" in r.text
    assert 'action="/logout"' in r.text
    assert "reader" in r.text
```

- [ ] **Step 2: 실패 확인**

Run: `.venv/bin/python -m pytest tests/test_auth.py::test_index_shows_user_and_logout -v`
Expected: FAIL (헤더에 로그아웃 폼 없음; 또한 `render_index`가 user 인자를 안 받음).

- [ ] **Step 3: `INDEX_CSS`에 로그아웃 스타일 추가**

`server.py`의 `INDEX_CSS` 문자열 끝, `footer{...}` 규칙 **다음 줄**(닫는 `"""` 직전)에 추가:

```css
  .logout-form{display:flex; align-items:center; gap:10px; margin:0;}
  .who{color:var(--muted); font-size:.82rem;}
  .logout-btn{font:inherit; font-size:.82rem; font-weight:700; color:var(--ink-2);
              background:transparent; border:1px solid var(--line); border-radius:999px;
              padding:6px 12px; cursor:pointer;}
  .logout-btn:hover{border-color:var(--red); color:var(--red);}
```

- [ ] **Step 4: `render_index` 시그니처와 헤더 수정**

`server.py`에서 `def render_index(docs: list) -> str:`를 다음으로 변경:

```python
def render_index(docs: list, user: str) -> str:
```

그리고 같은 함수의 반환 HTML 안 헤더 블록을 교체한다.

변경 전:
```python
    <header class="top">
      <span class="brand">CREFLE <span class="dot">Reports</span></span>
      <span class="count">{count}건</span>
      <a class="upload-link" href="/upload">+ 업로드</a>
    </header>
```

변경 후:
```python
    <header class="top">
      <span class="brand">CREFLE <span class="dot">Reports</span></span>
      <span class="count">{count}건</span>
      <a class="upload-link" href="/upload">+ 업로드</a>
      <form class="logout-form" method="post" action="/logout">
        <span class="who">{html.escape(user)}</span>
        <button class="logout-btn" type="submit">로그아웃</button>
      </form>
    </header>
```

- [ ] **Step 5: `index` 라우트가 user를 넘기도록 수정**

`server.py`의 index 라우트를 변경.

변경 전:
```python
@app.get("/", response_class=HTMLResponse)
def index(_: str = Depends(verify)) -> HTMLResponse:
    return HTMLResponse(render_index(discover_documents()))
```

변경 후:
```python
@app.get("/", response_class=HTMLResponse)
def index(user: str = Depends(verify)) -> HTMLResponse:
    return HTMLResponse(render_index(discover_documents(), user))
```

- [ ] **Step 6: 통과 확인**

Run: `.venv/bin/python -m pytest tests/test_auth.py -v`
Expected: 전부 PASS.

- [ ] **Step 7: 커밋**

```bash
git add server.py tests/test_auth.py
git commit -m "feat(ui): add user + logout button to index header"
```

---

## Task 6: lifespan 경고 + 모듈 docstring + 서빙 파일 회귀 테스트

**Files:**
- Modify: `server.py` (lifespan, 모듈 docstring)
- Test: `tests/test_auth.py`

- [ ] **Step 1: 서빙 파일 Basic 회귀 테스트 작성(register_report.sh 보호)**

`tests/test_auth.py`에 추가:

```python
@pytest.fixture
def served_doc():
    import shutil
    d = server.UPLOADS_DOCS / "demo" / "pytest_doc_v1"
    d.mkdir(parents=True, exist_ok=True)
    (d / "index.html").write_text("<title>pytest</title>ok", encoding="utf-8")
    url = "/" + (d / "index.html").relative_to(server.BASE_DIR).as_posix()
    yield url
    shutil.rmtree(server.UPLOADS_DOCS / "demo" / "pytest_doc_v1", ignore_errors=True)


def test_basic_serves_file_regression(served_doc):
    # register_report.sh 의 `curl -u ...` 반영확인과 동일 경로(Basic 헤더 → serve)
    r = client.get(served_doc, auth=("reader", "readerpass"))
    assert r.status_code == 200


def test_serve_unauth_browser_redirects(served_doc):
    r = client.get(served_doc, headers={"accept": "text/html"}, follow_redirects=False)
    assert r.status_code == 303
```

- [ ] **Step 2: 실행(이미 통과해야 정상 — serve 경로는 Task 3에서 verify로 동작)**

Run: `.venv/bin/python -m pytest tests/test_auth.py -k regression -v`
Expected: PASS (회귀 보호 확인). 만약 실패하면 serve 라우트가 새 `verify`를 쓰는지 확인.

- [ ] **Step 3: lifespan에 임시 키 경고 추가**

`server.py`의 `lifespan` 함수에서 `if _USING_DEFAULT_PASS:` 경고 블록 **아래**에 추가:

```python
    if _USING_EPHEMERAL_KEY:
        logger.warning("⚠️  REPORTS_SECRET_KEY 미설정 → 임시 키 사용(재시작 시 모든 로그인 무효화). 운영 전 설정하세요.")
```

- [ ] **Step 4: 모듈 docstring 갱신**

`server.py` 최상단 docstring의 "라우트" 목록과 "환경변수" 목록을 갱신한다. "라우트" 블록에 다음 줄을 추가(기존 `GET /upload` 줄 위/아래 적절 위치):

```
    GET  /login       로그인 폼 (무인증)
    POST /login       자격증명 검증 → JWT 쿠키 발급
    POST /logout      JWT 쿠키 삭제(로그아웃)
```

그리고 인증 설명 줄을 다음으로 교체:

변경 전:
```
/healthz 외 읽기는 Basic Auth(verify), 쓰기(/upload)는 별도 자격증명(require_uploader).
```
변경 후:
```
/healthz 외 읽기는 verify(JWT 쿠키 또는 Basic 헤더), 쓰기(/upload)는 require_uploader(uploader 역할).
브라우저는 /login 으로 로그인해 JWT 쿠키를 받고 /logout 으로 비운다. Basic 헤더는 자동화(register_report.sh)용 폴백으로 유지된다.
```

"환경변수" 블록 끝에 추가:
```
    REPORTS_SECRET_KEY                     JWT 서명 키 (미설정 시 임시 키 + 경고)
    REPORTS_TOKEN_TTL                      토큰 수명 초 (1209600=14일)
    REPORTS_COOKIE_SECURE                  TLS 뒤 1, 평문 HTTP 0 (기본 0)
```

- [ ] **Step 5: 전체 테스트 통과 확인**

Run: `.venv/bin/python -m pytest tests/test_auth.py -v`
Expected: 전부 PASS.

- [ ] **Step 6: 커밋**

```bash
git add server.py tests/test_auth.py
git commit -m "feat(auth): ephemeral-key warning, docstring, serve-file regression tests"
```

---

## Task 7: 운영/배포 설정 + README

**Files:**
- Modify: `.env.example`, `docker-compose.yml`, `README.md`

- [ ] **Step 1: `.env.example`에 JWT env 추가**

`.env.example` 끝에 추가:

```
# 세션(JWT) 서명 키 — 반드시 강한 무작위 값(예: openssl rand -hex 32). 미설정 시 임시 키(재시작마다 로그아웃).
REPORTS_SECRET_KEY=__SET_A_RANDOM_HEX_SECRET__
# 토큰 수명(초). 기본 1209600(14일).
REPORTS_TOKEN_TTL=1209600
# HTTPS(TLS) 뒤에서 1, 평문 HTTP(:28080)면 0.
REPORTS_COOKIE_SECURE=0
```

- [ ] **Step 2: `docker-compose.yml`에 env 전달 + 이미지 태그 bump**

`reporter` 서비스의 `environment:` 아래, `REPORTS_UPLOAD_PASS` 줄 다음에 추가:

```yaml
      # 세션(JWT) — SECRET_KEY 미설정 시 기동 거부(임시 키로 운영 시 매 재시작마다 전원 로그아웃 방지).
      REPORTS_SECRET_KEY: "${REPORTS_SECRET_KEY:?set a random hex secret for JWT signing}"
      REPORTS_TOKEN_TTL: "${REPORTS_TOKEN_TTL:-1209600}"
      REPORTS_COOKIE_SECURE: "${REPORTS_COOKIE_SECURE:-0}"
```

그리고 이미지 태그를 변경:

변경 전:
```yaml
    image: hub.crefle.com/service/reporter:1.2
```
변경 후:
```yaml
    image: hub.crefle.com/service/reporter:1.3
```

- [ ] **Step 3: `docker-compose.yml` 검증**

Run: `REPORTS_UPLOAD_PASS=x REPORTS_SECRET_KEY=y docker compose config >/dev/null && echo OK`
Expected: `OK` (YAML/보간 유효). docker 미설치 환경이면 이 단계는 건너뛰고 수동 확인.

- [ ] **Step 4: README 갱신**

`README.md`의 10~50행 구간을 읽고 다음을 반영한다:
- "접근 제한" 설명(12행 부근)을 교체:
  변경 전:
  ```
  - **접근 제한**: 모든 경로가 HTTP Basic Auth(아이디/비밀번호)로 보호됩니다.
  ```
  변경 후:
  ```
  - **접근 제한**: 사람은 `/login` 으로 로그인해 JWT 쿠키를 받고 `/logout` 으로 로그아웃합니다. 자동화/CLI(`register_report.sh` 등)는 `Authorization: Basic` 헤더로 계속 접근합니다(하이브리드).
  ```
- 환경변수 표(41~47행 부근)에 행 추가:
  ```
  | `REPORTS_SECRET_KEY` | (임시 키) | JWT 서명 키 — **운영 시 반드시 강한 무작위 값**(`openssl rand -hex 32`) |
  | `REPORTS_TOKEN_TTL` | `1209600` | 로그인 토큰 수명(초, 14일) |
  | `REPORTS_COOKIE_SECURE` | `0` | TLS 뒤에서 `1` |
  ```
- 배포 절차 섹션이 있으면, hulk `.env`에 `REPORTS_SECRET_KEY`를 추가해야 compose가 기동된다는 주의 + 이미지 태그가 `1.3`임을 명시.

- [ ] **Step 5: 커밋**

```bash
git add .env.example docker-compose.yml README.md
git commit -m "ops: pass JWT env to compose (image 1.3) + document login/logout"
```

---

## Task 8: 최종 검증 (전체 테스트 + 수동 스모크)

**Files:** (없음 — 검증 전용)

- [ ] **Step 1: 전체 테스트 스위트 실행**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: 전부 PASS, 실패 0.

- [ ] **Step 2: 서버 기동(수동 스모크용, 임시 포트)**

Run:
```bash
REPORTS_USER=reader REPORTS_PASS=readerpass \
REPORTS_UPLOAD_USER=uploader REPORTS_UPLOAD_PASS=uploaderpass \
REPORTS_SECRET_KEY=smoke-secret PORT=8099 \
.venv/bin/python server.py &
sleep 2
```
Expected: 기동 로그 출력. 임시 키 경고는 안 떠야 함(SECRET_KEY 설정했으므로).

- [ ] **Step 3: 미인증 브라우저 → 303 /login 확인**

Run: `curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' -H 'Accept: text/html' http://127.0.0.1:8099/`
Expected: `303 .../login`

- [ ] **Step 4: Basic 폴백(자동화) → 200 확인**

Run: `curl -s -o /dev/null -w '%{http_code}\n' -u reader:readerpass http://127.0.0.1:8099/`
Expected: `200`

- [ ] **Step 5: 로그인 → 쿠키 → 접근 → 로그아웃 흐름 확인**

Run:
```bash
curl -s -c /tmp/cj.txt -o /dev/null -w 'login=%{http_code}\n' \
  --data 'username=reader&password=readerpass&next=/' http://127.0.0.1:8099/login
curl -s -b /tmp/cj.txt -o /dev/null -w 'index=%{http_code}\n' -H 'Accept: text/html' http://127.0.0.1:8099/
curl -s -b /tmp/cj.txt -c /tmp/cj.txt -o /dev/null -w 'logout=%{http_code}\n' -X POST http://127.0.0.1:8099/logout
curl -s -b /tmp/cj.txt -o /dev/null -w 'after=%{http_code}\n' -H 'Accept: text/html' http://127.0.0.1:8099/
```
Expected: `login=303`, `index=200`, `logout=303`, `after=303`(쿠키 삭제됨 → 다시 로그인 필요).

- [ ] **Step 6: 서버 종료 + 정리**

Run: `kill %1 2>/dev/null; rm -f /tmp/cj.txt`
Expected: 백그라운드 서버 종료.

- [ ] **Step 7: 최종 상태 확인 후 마무리**

Run: `git status && git log --oneline -10`
Expected: 워킹트리 clean, Task 1~7 커밋 존재. 이후 `superpowers:finishing-a-development-branch`로 병합/PR 결정.

---

## Self-Review (작성자 점검 완료)

- **Spec 커버리지:** §5.1 인증모델→T3 · §5.2 라우트→T4 · §5.3 JWT/env→T2·T7 · §5.4 UI(로그인폼·헤더·JS poison)→T4·T5 · §5.5 운영→T7 · §6 테스트→T1~T6 · §7 위험(alg 고정·open-redirect·전환기 poison·회귀)→T2·T4·T6. 누락 없음.
- **Placeholder 스캔:** 모든 코드 스텝에 실제 코드 포함. TBD/TODO 없음.
- **타입 일관성:** `_make_token`/`_decode_token`/`_role_for_credentials`/`_identify`/`verify`/`require_uploader`/`render_index(docs, user)`/`render_login_form(...)`/`COOKIE_NAME`/`SECRET_KEY`/`TOKEN_TTL`/`COOKIE_SECURE` 시그니처가 정의·사용처 전반에서 일치.
