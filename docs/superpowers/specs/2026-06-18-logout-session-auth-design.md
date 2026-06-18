# 로그아웃 기능 설계 — JWT 무상태(stateless) 인증 전환 (하이브리드)

- 날짜: 2026-06-18
- 대상: `server.py` (인증), `requirements.txt`, `docker-compose.yml`, `.env.example`, `README.md`, `tests/`
- 상태: 승인됨(설계, JWT 개정) → 구현 계획 작성 예정

## 1. 배경 / 문제

CREFLE Reports 서버는 모든 읽기 경로를 **HTTP Basic Auth**(`verify`), 쓰기(`/upload`)를
별도 Basic 자격증명(`require_uploader`)으로 보호한다(`server.py:99–128`).

Basic Auth는 브라우저가 자격증명을 캐시해 매 요청마다 자동 전송하므로, 클라이언트가 "로그아웃"할
수단이 없다. "긴급 요청"인 **로그아웃 기능**을 구현하려면 클라이언트가 비울 수 있는 토큰 기반
인증으로 전환해야 한다. 본 설계는 **쿠키에 담은 무상태 JWT**(서버 세션 저장소 없음)를 도입하고,
로그아웃은 해당 쿠키 삭제로 구현한다.

## 2. 목표 / 비목표

**목표**
- 사람 사용자에게 **확실히 동작하는 로그아웃**을 제공한다(JWT 쿠키 삭제).
- **무상태**: 서버측 세션 저장소를 두지 않는다(JWT 자체 검증).
- 기존 자동화(`register_report.sh`의 반영확인 `curl -u`)와 렌더러를 **깨뜨리지 않는다.**

**비목표(별도 과제로 보류 — 기존 보안 보류 정책, 커밋 719cf9c와 일치)**
- **JWT 서버측 강제 무효화(denylist/revocation)** — 상태를 재도입하므로 무상태 목표와 상충.
- 로그인 브루트포스 rate-limit
- 약한 기본 `REPORTS_PASS=crefle` 교체
- Google Workspace OAuth 전환(`server.py:116` 주석의 향후 방향)

## 3. 영향 범위 조사 결과 (사전 확인 완료)

- **렌더러 워커**(`renderer/worker.py`): `network_mode: none`, 파일시스템 큐 폴링 + `file://`
  렌더. HTTP 미사용 → 인증 변경 **영향 없음**.
- **`register_report.sh:186`**: rsync 후 `curl --path-as-is -s -u "$U:$P" "$BASE_URL/$ENC"` →
  `200` 기대. 명시적 `Authorization: Basic` 헤더를 보냄 → **하이브리드로 Basic을 유지하면 무수정 동작**.
- **docker healthcheck**: `/healthz`(무인증) → 영향 없음.

## 4. 결정 사항

| 항목 | 결정 |
|------|------|
| 인증 방식 | 쿠키에 담은 **JWT 로그인/로그아웃** (무상태) |
| 토큰 검증 | **JWT HS256 서명 + `exp` 만료** 자체 검증 — 서버 저장소 없음 |
| 기존 Basic Auth | **하이브리드 유지** — 명시적 `Authorization` 헤더 클라이언트(자동화/CLI)용 폴백 |
| 역할 | 기존 2단계(`reader`/`uploader`) 유지 — JWT `role` 클레임에 보관 |

## 5. 설계

### 5.1 인증 모델 (핵심 seam)

요청은 **둘 중 하나면** 인증 통과:
1. 유효한 **JWT 쿠키**(서명·`exp` 검증 통과, 클레임 `role` ∈ {reader, uploader}), 또는
2. 유효한 **`Authorization: Basic` 헤더**(reader/uploader 자격증명 매칭) — 자동화 폴백.

의존성 동작:
- `verify`(읽기): JWT role ∈ {reader, uploader} **또는** Basic이 reader/uploader 매칭 → 통과.
- `require_uploader`(쓰기): JWT role == uploader **또는** Basic이 uploader 매칭 → 통과.
  `UPLOAD_PASS` 미설정 시 **503 fail-closed** 유지.
- Basic 자격증명 비교는 `secrets.compare_digest`(상수시간) 유지. uploader를 먼저 검사해
  강한 계정이 업로드 권한을 갖게 한다.

**인증 실패 분기**(`Accept` 헤더로 클라이언트 판별):
- 브라우저(`Accept`에 `text/html` 포함) → `303 /login?next=<원경로>` 리다이렉트.
  **`WWW-Authenticate: Basic` 헤더를 더는 보내지 않는다** → 브라우저가 Basic 다이얼로그를
  띄우지 않고 Basic 자격증명을 캐시하지 않음 → **JWT 쿠키 삭제(로그아웃)가 확실히 동작**.
- 비-브라우저(자동화/API) → `401 JSON`. `register_report.sh`는 올바른 `-u`를 보내므로 200.

구현 메모: 의존성에 `request: Request`를 주입해 쿠키/`Accept`/경로를 읽는다. 브라우저 미인증 시
커스텀 예외(`NeedsLogin(next_url)`)를 raise → `@app.exception_handler(NeedsLogin)`에서
`RedirectResponse(.../login?next=..., 303)` 반환. 비-브라우저 미인증 시 `HTTPException(401)`.
JWT 디코드는 만료(`ExpiredSignatureError`)·서명오류(`InvalidTokenError`)를 미인증으로 처리.

### 5.2 라우트

| 라우트 | 인증 | 동작 |
|--------|------|------|
| `GET /healthz` | 없음 | (변경 없음) |
| `GET /login` | 공개 | 로그인 폼. 이미 인증돼 있으면 `next`/`/`로 리다이렉트 |
| `POST /login` | 공개 | username/password 검증(uploader 먼저). 성공 시 **JWT(sub=user, role, iat, exp) 발급 → HttpOnly 쿠키 set** 후 `303 next`. `next`는 동일출처 상대경로(`/`로 시작, `//`·스킴 불허)만 허용 → 오픈리다이렉트 차단. 실패 시 폼에 오류(401 본문) |
| `POST /logout` | — | **JWT 쿠키 삭제**(`delete_cookie`) 후 `303 /login?loggedout=1` |
| `GET /` | `verify` | (인증 모델만 교체) 헤더에 사용자·로그아웃 버튼 |
| `GET /upload`,`POST /upload` | `require_uploader` | (인증 모델만 교체) |
| `GET /{full_path}` | `verify` | (인증 모델만 교체) |

### 5.3 토큰(JWT) 메커니즘 / 설정

- **PyJWT**로 HS256 대칭 서명. 클레임: `{sub: username, role: reader|uploader, iat, exp}`.
  디코드 시 **`algorithms=["HS256"]` 명시**(alg-confusion·`alg:none` 방어), `exp` 자동 검증.
- 서버 저장소 없음 → 멀티워커·재시작 무관(서명 키만 공유되면 검증 가능).
- 신규 환경변수:
  - `REPORTS_SECRET_KEY` — JWT 서명 키. 미설정 시 기동 시 임시 랜덤(`secrets.token_hex(32)`)
    생성 + 경고 로그(기존 `REPORTS_PASS` 경고 패턴과 동일). **임시 키는 재시작 시 기존 토큰
    무효화**되므로 운영에선 설정 권장.
  - `REPORTS_TOKEN_TTL` — 토큰 수명 초. 기본 `1209600`(14일). 짧을수록 유출 토큰 잔존 위험↓.
  - `REPORTS_COOKIE_SECURE` — 기본 `0`(:28080 평문 HTTP). TLS 뒤에선 `1`.
- 쿠키: 이름 `reports_token`, `HttpOnly`, `SameSite=Lax`(`/login`·`/logout` POST CSRF 완화),
  `Max-Age=REPORTS_TOKEN_TTL`, `Secure=REPORTS_COOKIE_SECURE`, `Path=/`.
- `requirements.txt`에 `pyjwt>=2.0` 추가. (SessionMiddleware/`itsdangerous` 미사용.)

### 5.4 UI (기존 `INDEX_CSS` 디자인 시스템 재사용)

- `render_login_form(error=None, next_url="/", loggedout=False)`: 중앙 카드(아이디/비번/오류
  슬롯) + CREFLE 브랜딩. `render_upload_form`과 톤 일치(필요 시 `LOGIN_CSS` 소량 추가).
- `render_index(docs, user)`: 헤더에 `{user} · [로그아웃]`(`.upload-link` 스타일의 작은
  `<form method="post" action="/logout">` 버튼) 추가. 시그니처에 `user` 추가.
- 전환기 보정(**포함**): `?loggedout=1` 도착 시, 구 시스템에서 캐시된 Basic 자격증명을
  비우는 JS 1줄(잘못된 자격증명으로 보호 경로 XHR → 캐시 eviction). 구 사용자도 즉시 로그아웃 보장.

### 5.5 운영/배포

- `docker-compose.yml`: 신규 env 3개(`REPORTS_SECRET_KEY`/`REPORTS_TOKEN_TTL`/
  `REPORTS_COOKIE_SECURE`) 전달, 이미지 태그 `reporter:1.2 → 1.3`. 렌더러 이미지 불변.
- `.env.example`: 신규 env 추가(SECRET_KEY 강한 값 안내).
- `README.md` + `server.py` docstring: 로그인/로그아웃 흐름, JWT 신규 env, "Basic은 자동화용으로
  유지" 명시.
- 실제 배포는 기존 흐름(hub.crefle.com 빌드·푸시 → hulk pull·recreate)으로 별도 수행
  (메모 `reporter-hulk-deploy.md`).

## 6. 테스트 (현재 테스트 없음 → 신규)

`tests/test_auth.py`(FastAPI `TestClient`, dev 의존성 `httpx`):
- `/healthz` 무인증 200.
- 미인증 브라우저(`Accept: text/html`) `/` → 303 `Location: /login...`.
- Basic reader 헤더 `/` → 200(자동화 폴백).
- `POST /login`(reader) → JWT 쿠키 설정, 이후 `/` → 200.
- `POST /login` 오답 → 쿠키 없음, 오류.
- **변조/만료 토큰 쿠키** → 미인증 처리(브라우저 303 / API 401).
- reader 토큰의 `GET /upload` → 거부(403/redirect), uploader 토큰 → 200.
- `POST /logout` → 쿠키 삭제, 이후 브라우저 `/` → 303 /login.
- `curl -u` 동등(Basic 헤더)로 서빙 파일 → 200(`register_report.sh` 회귀 방지).

## 7. 위험 / 완화

- **무상태 JWT 무효화 불가**: 발급된 토큰은 `exp` 전 서버측 강제 무효화 불가(denylist는 상태
  재도입 → 비목표). 완화: 짧은 `REPORTS_TOKEN_TTL`. 일반 사용처인 브라우저 로그아웃(쿠키
  삭제)은 정상 동작하므로 긴급 요구사항은 충족. 키 교체(`REPORTS_SECRET_KEY` 변경)는 전체
  토큰을 일괄 무효화하는 비상수단으로 사용 가능.
- **전환기 Basic 캐시**: 구 시스템에서 Basic을 캐시한 브라우저는 하이브리드 폴백으로 자동
  인증될 수 있음 → §5.4 JS poison으로 로그아웃 시 캐시 eviction.
- **알고리즘 혼동**: `jwt.decode`에 `algorithms=["HS256"]`을 명시해 `alg:none`·비대칭 혼동 차단.
- **오픈리다이렉트**: `next` 파라미터를 동일출처 상대경로로 엄격 검증.
- **CSRF(login/logout POST)**: `SameSite=Lax` + 동일출처 폼으로 완화.
- **회귀**: `register_report.sh`·렌더러·healthcheck 무영향 — §3 + §6 테스트로 보장.
