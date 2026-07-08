# 매일 조건부 자동 배포 (CI/CD) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 매일 17:00 KST에 main이 지난 배포 이후 바뀐 경우에만, hulk의 self-hosted runner가 proposals를 동기화하고 코드 변경 시 이미지를 재빌드·Harbor push·재배포한다(테스트 게이트·헬스체크·자동 롤백 포함).

**Architecture:** GitHub Actions는 스케줄·게이팅만 담당하고 실제 작업은 `runs-on: [self-hosted, hulk]` 러너가 hulk 내부에서 로컬로 수행한다. 배포 로직은 `ops/deploy.sh`(멱등, `DRY_RUN` 지원)에 두고 워크플로는 얇게 유지한다. 이미지 태그를 compose에서 env 변수화해 immutable short-SHA로 배포·롤백한다.

**Tech Stack:** GitHub Actions (self-hosted runner, systemd), Bash, Docker/Compose v2, Harbor(hub.crefle.com), pytest(python:3.12-slim 컨테이너).

## Global Constraints

- 레지스트리: `hub.crefle.com`, 이미지 `service/reporter`(뷰어) · `service/reporter-renderer`(렌더러). 값은 `REGISTRY` 기본 `hub.crefle.com`.
- 배포 디렉터리: `/home/hulk/working/reporter.crefle.com` (`DEPLOY_DIR` 기본값).
- 헬스체크: `http://127.0.0.1:28080/healthz` 200.
- 러너: 라벨 `hulk`, 워크플로 `runs-on: [self-hosted, hulk]`, hulk 유저로 실행(docker 그룹·Harbor 로그인·배포 디렉터리 로컬 접근).
- 스케줄: `cron: '0 8 * * *'` (UTC = 17:00 KST). 지연 허용.
- 이미지 태그: **immutable short SHA**(`git rev-parse --short HEAD`). compose 기본 폴백은 현재 태그(뷰어 `1.6`, 렌더러 `1.1`)로 유지 → 기존 수동 절차 무손상.
- 테스트는 반드시 `python:3.12-slim` 컨테이너에서 실행(hulk 시스템 Python은 3.8).
- 트리거는 `schedule` + `workflow_dispatch` 뿐(push·PR 트리거 금지 — self-hosted 러너에 신뢰불가 코드 실행 방지).
- 스펙: `docs/superpowers/specs/2026-07-08-cicd-daily-deploy-design.md`.

---

### Task 1: compose 이미지 태그 env 변수화 + .env.example

**Files:**
- Modify: `docker-compose.yml:3` (뷰어 image), `docker-compose.yml:37` (렌더러 image)
- Modify: `.env.example` (레지스트리·태그 변수 추가)

**Interfaces:**
- Produces: compose가 `${REGISTRY:-hub.crefle.com}`, `${REPORTER_TAG:-1.6}`, `${RENDERER_TAG:-1.1}` 변수를 소비. `ops/deploy.sh`(Task 2)가 배포 디렉터리 `.env`의 `REPORTER_TAG`/`RENDERER_TAG`를 갱신하면 반영됨.

- [ ] **Step 1: 뷰어 image 라인을 변수화**

`docker-compose.yml`에서:
```yaml
    image: hub.crefle.com/service/reporter:1.6
```
를 다음으로 변경:
```yaml
    image: ${REGISTRY:-hub.crefle.com}/service/reporter:${REPORTER_TAG:-1.6}
```

- [ ] **Step 2: 렌더러 image 라인을 변수화**

`docker-compose.yml`에서:
```yaml
    image: hub.crefle.com/service/reporter-renderer:1.1
```
를 다음으로 변경:
```yaml
    image: ${REGISTRY:-hub.crefle.com}/service/reporter-renderer:${RENDERER_TAG:-1.1}
```

- [ ] **Step 3: `.env.example`에 변수 추가**

`.env.example` 끝에 다음 블록을 추가:
```bash

# ── 이미지 태그 (CI/CD 자동 배포가 갱신). 수동 운영 시엔 미설정으로 두면 compose 기본값(1.6/1.1) 사용.
# 자동 배포는 빌드한 short-SHA 로 아래 값을 덮어써 immutable 배포/롤백한다.
REGISTRY=hub.crefle.com
REPORTER_TAG=1.6
RENDERER_TAG=1.1
```

- [ ] **Step 4: 기본값 렌더 검증 (변수 미설정 → 현재 태그 유지)**

Run:
```bash
REPORTS_UPLOAD_PASS=x REPORTS_SECRET_KEY=y docker compose config | grep -E 'image:'
```
Expected: 두 줄이 각각 `hub.crefle.com/service/reporter:1.6`, `hub.crefle.com/service/reporter-renderer:1.1` 로 렌더.

- [ ] **Step 5: 변수 주입 렌더 검증 (태그 오버라이드)**

Run:
```bash
REPORTS_UPLOAD_PASS=x REPORTS_SECRET_KEY=y REGISTRY=hub.crefle.com REPORTER_TAG=abc1234 RENDERER_TAG=def5678 \
  docker compose config | grep -E 'image:'
```
Expected: `…/service/reporter:abc1234`, `…/service/reporter-renderer:def5678`.

- [ ] **Step 6: Commit**

```bash
git add docker-compose.yml .env.example
git commit -m "ops(cicd): compose 이미지 태그 env 변수화 (REGISTRY/REPORTER_TAG/RENDERER_TAG)"
```

---

### Task 2: `ops/deploy.sh` 배포 로직 + DRY_RUN 테스트 하네스

**Files:**
- Create: `ops/deploy.sh`
- Create: `ops/deploy_test.sh` (로컬 검증 하네스; docker/hulk 불필요)

**Interfaces:**
- Consumes: 환경변수 `GITHUB_WORKSPACE`(checkout 경로), `DEPLOY_DIR`, `REGISTRY`, 선택 `HEALTH_URL`/`HEALTH_RETRIES`/`HEALTH_INTERVAL`/`DRY_RUN`. Task 1의 compose 태그 변수.
- Produces: `bash ops/deploy.sh` 진입점(Task 3 워크플로가 호출). 성공 시 `$DEPLOY_DIR/.deployed_sha`에 HEAD 기록. DRY_RUN=1이면 부작용 없이 `계획: viewer=.. renderer=.. proposals=.. compose=..` 라인과 `[dry-run] …` 명령 트레이스를 출력.

- [ ] **Step 1: `ops/deploy.sh` 작성**

`ops/deploy.sh` 전체 내용:
```bash
#!/usr/bin/env bash
# ops/deploy.sh — reporter 자동 배포 (hulk self-hosted runner에서 실행).
# 흐름: 변경 판정(.deployed_sha vs HEAD) → 경로 분류 → 테스트 게이트(3.12 컨테이너)
#       → 조건부 rsync/빌드/push → compose 배포 → 헬스체크 → 실패 시 롤백 → 성공 시 마커 기록.
# DRY_RUN=1 이면 docker/rsync/compose/헬스/마커를 실제 실행하지 않고 출력만 한다(로컬 검증용).
set -Eeuo pipefail

# ── 설정 (환경변수 주입, 기본값 있음) ──────────────────────────────
: "${GITHUB_WORKSPACE:=$(pwd)}"
DEPLOY_DIR="${DEPLOY_DIR:-/home/hulk/working/reporter.crefle.com}"
REGISTRY="${REGISTRY:-hub.crefle.com}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:28080/healthz}"
HEALTH_RETRIES="${HEALTH_RETRIES:-12}"
HEALTH_INTERVAL="${HEALTH_INTERVAL:-5}"
DRY_RUN="${DRY_RUN:-0}"

VIEWER_IMAGE="$REGISTRY/service/reporter"
RENDERER_IMAGE="$REGISTRY/service/reporter-renderer"
MARKER="$DEPLOY_DIR/.deployed_sha"
ENVFILE="$DEPLOY_DIR/.env"

log() { printf '[deploy] %s\n' "$*"; }
# run: DRY_RUN 이면 출력만, 아니면 인자를 그대로 실행(배열 → 공백/특수문자 안전). 복합 명령은 bash -c 로.
run() { if [ "$DRY_RUN" = "1" ]; then printf '[dry-run] %s\n' "$*"; else "$@"; fi; }

get_env() { grep -E "^$1=" "$ENVFILE" 2>/dev/null | tail -1 | cut -d= -f2- || true; }
set_env() {
  local key="$1" val="$2"
  if [ "$DRY_RUN" = "1" ]; then printf '[dry-run] set %s=%s in %s\n' "$key" "$val" "$ENVFILE"; return; fi
  if grep -qE "^$key=" "$ENVFILE" 2>/dev/null; then
    sed -i "s|^$key=.*|$key=$val|" "$ENVFILE"
  else
    printf '%s=%s\n' "$key" "$val" >> "$ENVFILE"
  fi
}
write_marker() {
  if [ "$DRY_RUN" = "1" ]; then printf '[dry-run] echo %s > %s\n' "$HEAD" "$MARKER"; else echo "$HEAD" > "$MARKER"; fi
}
healthcheck() {
  if [ "$DRY_RUN" = "1" ]; then printf '[dry-run] healthcheck %s\n' "$HEALTH_URL"; return 0; fi
  local i
  for i in $(seq 1 "$HEALTH_RETRIES"); do
    if curl -fsS -o /dev/null --max-time 5 "$HEALTH_URL"; then log "healthz OK (시도 $i)"; return 0; fi
    log "healthz 대기… ($i/$HEALTH_RETRIES)"; sleep "$HEALTH_INTERVAL"
  done
  return 1
}
matches() { printf '%s\n' "$CHANGED" | grep -qE "$1"; }

cd "$GITHUB_WORKSPACE"
HEAD="$(git rev-parse HEAD)"
SHORT="$(git rev-parse --short HEAD)"
OLD="$(cat "$MARKER" 2>/dev/null || true)"

# ── 1. 변경 판정 ───────────────────────────────────────────────────
if [ -n "$OLD" ] && [ "$OLD" = "$HEAD" ]; then
  log "변경 없음 (deployed=$OLD == HEAD). skip."
  exit 0
fi

# ── 2. 경로 분류 ───────────────────────────────────────────────────
FULL=0; CHANGED=""
if [ -z "$OLD" ]; then
  log "마커 없음 → 전체 배포(baseline)."; FULL=1
elif ! git cat-file -e "${OLD}^{commit}" 2>/dev/null; then
  log "이전 SHA($OLD) 히스토리에 없음 → 전체 배포."; FULL=1
else
  CHANGED="$(git diff --name-only "$OLD".."$HEAD")"
  log "변경 파일:"; printf '%s\n' "$CHANGED" | sed 's/^/  /'
fi

BUILD_VIEWER=0; BUILD_RENDERER=0; SYNC_PROPOSALS=0; COMPOSE_UP=0
if [ "$FULL" = "1" ]; then
  BUILD_VIEWER=1; BUILD_RENDERER=1; SYNC_PROPOSALS=1; COMPOSE_UP=1
else
  if matches '^(server\.py|uploads_handler\.py|shares\.py|requirements\.txt|Dockerfile)$'; then BUILD_VIEWER=1; COMPOSE_UP=1; fi
  if matches '^(Dockerfile\.renderer|tools/render_pdf\.py|renderer/worker\.py)$'; then BUILD_RENDERER=1; COMPOSE_UP=1; fi
  if matches '^docker-compose\.yml$'; then COMPOSE_UP=1; fi
  if matches '^proposals/'; then SYNC_PROPOSALS=1; fi
fi
log "계획: viewer=$BUILD_VIEWER renderer=$BUILD_RENDERER proposals=$SYNC_PROPOSALS compose=$COMPOSE_UP"

# 운영 반영 대상이 전혀 없으면(무시 경로만 변경) 마커만 전진시키고 종료.
if [ "$BUILD_VIEWER$BUILD_RENDERER$SYNC_PROPOSALS$COMPOSE_UP" = "0000" ]; then
  log "운영 반영 대상 변경 없음. 마커만 갱신."
  write_marker
  exit 0
fi

# ── 3. 테스트 게이트 (python:3.12-slim) ────────────────────────────
log "pytest (python:3.12-slim 컨테이너)…"
run docker run --rm -e PYTHONDONTWRITEBYTECODE=1 -v "$GITHUB_WORKSPACE":/w -w /w python:3.12-slim \
  bash -c "pip install -q -r requirements.txt -r requirements-dev.txt && pytest -q"

# ── 4. 콘텐츠 동기화 ───────────────────────────────────────────────
if [ "$SYNC_PROPOSALS" = "1" ]; then
  log "rsync proposals/ → $DEPLOY_DIR/proposals/"
  run rsync -az --delete "$GITHUB_WORKSPACE/proposals/" "$DEPLOY_DIR/proposals/"
fi

# ── 5. 조건부 빌드 + push (native amd64) ───────────────────────────
if [ "$BUILD_VIEWER" = "1" ]; then
  log "build+push 뷰어 $VIEWER_IMAGE:$SHORT"
  run docker build -t "$VIEWER_IMAGE:$SHORT" .
  run docker push "$VIEWER_IMAGE:$SHORT"
fi
if [ "$BUILD_RENDERER" = "1" ]; then
  log "build+push 렌더러 $RENDERER_IMAGE:$SHORT"
  run docker build -f Dockerfile.renderer -t "$RENDERER_IMAGE:$SHORT" .
  run docker push "$RENDERER_IMAGE:$SHORT"
fi

# ── 6. 배포 (compose) ──────────────────────────────────────────────
if [ "$COMPOSE_UP" = "1" ]; then
  PREV_VIEWER="$(get_env REPORTER_TAG)"; PREV_RENDERER="$(get_env RENDERER_TAG)"
  run cp "$GITHUB_WORKSPACE/docker-compose.yml" "$DEPLOY_DIR/docker-compose.yml"
  [ "$BUILD_VIEWER" = "1" ]   && set_env REPORTER_TAG "$SHORT"
  [ "$BUILD_RENDERER" = "1" ] && set_env RENDERER_TAG "$SHORT"
  log "docker compose pull && up -d"
  run bash -c "cd '$DEPLOY_DIR' && docker compose pull && docker compose up -d"

  # ── 7. 헬스체크 + 실패 시 롤백 ──────────────────────────────────
  if ! healthcheck; then
    log "헬스체크 실패 → 이전 태그로 롤백 (viewer=$PREV_VIEWER renderer=$PREV_RENDERER)."
    [ -n "$PREV_VIEWER" ]   && set_env REPORTER_TAG "$PREV_VIEWER"
    [ -n "$PREV_RENDERER" ] && set_env RENDERER_TAG "$PREV_RENDERER"
    run bash -c "cd '$DEPLOY_DIR' && docker compose up -d"
    log "롤백 완료. 마커 미갱신."
    exit 1
  fi
fi

# ── 8. 마커 기록 (성공 시에만) ─────────────────────────────────────
write_marker
log "배포 완료: $HEAD"
```

- [ ] **Step 2: 실행권한 부여**

Run: `chmod +x ops/deploy.sh`
Expected: 종료코드 0.

- [ ] **Step 3: 문법 검사 (bash -n, 가능하면 shellcheck)**

Run: `bash -n ops/deploy.sh && (command -v shellcheck >/dev/null && shellcheck -S warning ops/deploy.sh || echo "shellcheck 미설치 — 건너뜀")`
Expected: 출력 없음(문법 OK) 또는 "shellcheck 미설치". shellcheck 경고가 나오면 수정.

- [ ] **Step 4: DRY_RUN 테스트 하네스 `ops/deploy_test.sh` 작성**

`ops/deploy_test.sh` 전체 내용:
```bash
#!/usr/bin/env bash
# ops/deploy_test.sh — deploy.sh 의 판정/분류 로직 검증 (DRY_RUN; docker/hulk 불필요, git 만 필요).
set -Eeuo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
DEPLOY_SH="$HERE/ops/deploy.sh"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
REPO="$TMP/repo"; DEPLOY="$TMP/deploy"; mkdir -p "$REPO" "$DEPLOY"

cd "$REPO"; git init -q; git config user.email t@t; git config user.name t
mkdir -p proposals renderer tools
for f in server.py uploads_handler.py shares.py requirements.txt Dockerfile \
         Dockerfile.renderer tools/render_pdf.py renderer/worker.py \
         docker-compose.yml README.md proposals/doc.html; do echo a > "$f"; done
git add -A; git commit -qm base
# 배포 디렉터리에 .env 시드(get_env/set_env 대상)
printf 'REPORTER_TAG=1.6\nRENDERER_TAG=1.1\n' > "$DEPLOY/.env"

fail() { echo "FAIL: $1"; echo "---- output ----"; echo "$2"; exit 1; }
assert_has() { echo "$2" | grep -qF "$1" || fail "expected '$1'" "$2"; }
plan() { # $1=marker_sha("" none). DRY 실행 결과 출력.
  if [ -n "$1" ]; then echo "$1" > "$DEPLOY/.deployed_sha"; else rm -f "$DEPLOY/.deployed_sha"; fi
  DRY_RUN=1 DEPLOY_DIR="$DEPLOY" GITHUB_WORKSPACE="$REPO" REGISTRY=hub.crefle.com bash "$DEPLOY_SH"
}
commit_change() { git commit -qam "$1"; git rev-parse HEAD; }

n=0
# 1) 마커 없음 → 전체 배포
out="$(plan "")"; assert_has "viewer=1 renderer=1 proposals=1 compose=1" "$out"; n=$((n+1))
# 2) 마커==HEAD → skip
BASE="$(git rev-parse HEAD)"; out="$(plan "$BASE")"; assert_has "변경 없음" "$out"; n=$((n+1))
# 3) proposals 만 → rsync 만
P="$(git rev-parse HEAD)"; echo b > proposals/doc.html; H=$(commit_change p)
out="$(plan "$P")"; assert_has "viewer=0 renderer=0 proposals=1 compose=0" "$out"; n=$((n+1))
# 4) server.py → 뷰어 재빌드 + compose
P="$H"; echo b > server.py; H=$(commit_change s)
out="$(plan "$P")"; assert_has "viewer=1 renderer=0 proposals=0 compose=1" "$out"; n=$((n+1))
# 5) Dockerfile.renderer → 렌더러 재빌드 + compose
P="$H"; echo b > Dockerfile.renderer; H=$(commit_change r)
out="$(plan "$P")"; assert_has "viewer=0 renderer=1 proposals=0 compose=1" "$out"; n=$((n+1))
# 6) docker-compose.yml 만 → compose 만
P="$H"; echo b > docker-compose.yml; H=$(commit_change c)
out="$(plan "$P")"; assert_has "viewer=0 renderer=0 proposals=0 compose=1" "$out"; n=$((n+1))
# 7) README.md 만 → 무시(마커만 갱신)
P="$H"; echo b > README.md; H=$(commit_change d)
out="$(plan "$P")"; assert_has "마커만 갱신" "$out"; n=$((n+1))
# 8) 코드+콘텐츠 동시 → 둘 다
P="$H"; echo c > server.py; echo c > proposals/doc.html; H=$(commit_change m)
out="$(plan "$P")"; assert_has "viewer=1 renderer=0 proposals=1 compose=1" "$out"; n=$((n+1))

echo "OK — $n 시나리오 통과"
```

- [ ] **Step 5: 하네스 실행권한 + 실행 (실패 확인 전에 먼저 실패하는지 관찰)**

Run: `chmod +x ops/deploy_test.sh && bash ops/deploy_test.sh`
Expected: `OK — 8 시나리오 통과`. (만약 실패하면 deploy.sh 분류 로직을 수정 후 재실행.)

- [ ] **Step 6: Commit**

```bash
git add ops/deploy.sh ops/deploy_test.sh
git commit -m "ops(cicd): 자동 배포 스크립트 deploy.sh (판정·빌드·배포·헬스·롤백) + DRY 테스트 하네스"
```

---

### Task 3: GitHub Actions 워크플로 `deploy.yml`

**Files:**
- Create: `.github/workflows/deploy.yml`

**Interfaces:**
- Consumes: `ops/deploy.sh`(Task 2). self-hosted 러너 라벨 `hulk`(Task 5에서 등록).
- Produces: 매일 08:00 UTC 및 수동(`workflow_dispatch`) 실행되는 `daily-deploy` 워크플로.

- [ ] **Step 1: 워크플로 작성**

`.github/workflows/deploy.yml` 전체 내용:
```yaml
name: daily-deploy

on:
  schedule:
    - cron: '0 8 * * *'   # 08:00 UTC = 17:00 KST (지연 허용)
  workflow_dispatch:       # 수동 실행 / 롤백 / 테스트

# 동시 실행 방지(겹치면 앞선 실행을 기다림, 취소하지 않음).
concurrency:
  group: daily-deploy
  cancel-in-progress: false

jobs:
  deploy:
    runs-on: [self-hosted, hulk]
    timeout-minutes: 30
    steps:
      - name: Checkout (full history — SHA 게이팅 diff 용)
        uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - name: Deploy
        run: bash ops/deploy.sh
        env:
          DEPLOY_DIR: /home/hulk/working/reporter.crefle.com
          REGISTRY: hub.crefle.com
```

- [ ] **Step 2: YAML 문법 검증**

Run:
```bash
python -c "import yaml,sys; yaml.safe_load(open('.github/workflows/deploy.yml')); print('yaml OK')"
```
Expected: `yaml OK`.

- [ ] **Step 3: (선택) actionlint 로 워크플로 검증**

Run: `command -v actionlint >/dev/null && actionlint .github/workflows/deploy.yml || echo "actionlint 미설치 — 건너뜀"`
Expected: 출력 없음(문제 없음) 또는 "actionlint 미설치". 오류가 나오면 수정.

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/deploy.yml
git commit -m "ci(cicd): 매일 17:00 KST 조건부 자동 배포 워크플로 (self-hosted hulk)"
```

---

### Task 4: README 문서화 (CI/CD 섹션 + 러너 설치 · 롤백 · COPY 트랩)

**Files:**
- Modify: `README.md` ("운영 배포 (hulk · Docker)" 섹션 뒤에 "CI/CD 자동 배포" 하위 섹션 추가)

**Interfaces:**
- Consumes: Task 1~3 산출물(워크플로·deploy.sh·태그 변수).

- [ ] **Step 1: "운영 배포" 섹션 끝(리포트 추가/갱신 문단 뒤)에 CI/CD 하위 섹션 추가**

`README.md`의 "### 리포트 추가/갱신 (재빌드·재시작 불필요)" 문단 바로 뒤에 다음을 삽입:
```markdown
### CI/CD 자동 배포 (매일 17:00 KST · GitHub Actions)

`.github/workflows/deploy.yml` 이 매일 08:00 UTC(=17:00 KST)와 수동(`workflow_dispatch`) 시,
**hulk 내부 self-hosted runner**(`runs-on: [self-hosted, hulk]`)에서 `ops/deploy.sh` 를 실행한다.
GitHub 클라우드는 사내망 hulk/Harbor에 접근할 수 없으므로 실제 빌드·배포는 hulk 로컬에서 일어난다.

- **변경 판정**: 배포 디렉터리의 `.deployed_sha` 마커와 main HEAD 를 비교. 같으면 skip.
  다르면 `git diff` 로 경로를 분류해 **proposals 만 바뀌면 rsync**, **코드가 바뀌면 해당 이미지 재빌드**.
- **게이트**: 배포 전 `python:3.12-slim` 컨테이너에서 pytest 실행, 실패 시 배포 중단.
- **태그**: 빌드는 immutable `:<short-sha>` 로 push 하고 `.env` 의 `REPORTER_TAG`/`RENDERER_TAG` 를 갱신.
- **헬스체크·롤백**: 배포 후 `/healthz` 확인, 실패 시 이전 태그로 자동 롤백(마커 미갱신).
- **실패 알림**: GitHub Actions 실패 시 repo watcher 에게 이메일.

**러너 설치 (hulk, 최초 1회):**
```bash
mkdir -p ~/actions-runner && cd ~/actions-runner
# 최신 러너 버전은 https://github.com/actions/runner/releases 참조
curl -o actions-runner-linux-x64.tar.gz -L \
  https://github.com/actions/runner/releases/download/v2.XYZ/actions-runner-linux-x64-2.XYZ.tar.gz
tar xzf actions-runner-linux-x64.tar.gz
# 등록 토큰: GitHub repo Settings → Actions → Runners → New self-hosted runner 에서 발급
./config.sh --url https://github.com/CREFLEINC/reports --token <REG_TOKEN> --labels hulk --unattended
sudo ./svc.sh install hulk && sudo ./svc.sh start   # systemd 상시화(재부팅 자동 복구)
```

**수동 배포/롤백:**
- 수동 실행: GitHub Actions → daily-deploy → Run workflow.
- 롤백: 배포 디렉터리 `.env` 의 `REPORTER_TAG`/`RENDERER_TAG` 를 직전 SHA 로 바꾸고
  `docker compose up -d`. (실패 배포는 자동 롤백되므로 보통 불필요.)

> ⚠️ **COPY 트랩**: 새 최상위 파이썬 모듈(예: `foo.py`)을 추가하면 반드시 `Dockerfile` 의
> `COPY server.py uploads_handler.py shares.py ./` 줄에 추가할 것. 빠뜨리면 이미지에 모듈이 없어
> 컨테이너가 import 에서 크래시한다. 자동 배포의 헬스체크가 이를 잡아 롤백하지만, 원인 수정이 필요하다.
```

- [ ] **Step 2: 렌더 확인**

Run: `grep -n "CI/CD 자동 배포" README.md`
Expected: 삽입한 헤더 라인 번호가 출력됨.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs(cicd): README 에 자동 배포·러너 설치·롤백·COPY 트랩 안내 추가"
```

---

### Task 5: (Ops · 사용자 협조) 러너 등록 + hulk `.env` 시드 + 라이브 검증

> 이 태스크는 코드 커밋이 아니라 **hulk 서버 1회 셋업 + 라이브 배포 검증**이다. 러너 등록 토큰은
> GitHub repo Settings 에서 사용자가 발급해야 하므로, 해당 단계는 사용자 수행이 필요하다.

**Files:** (없음 — 서버 셋업)

- [ ] **Step 1: hulk `.env` 에 태그 변수 시드 (현재 운영 태그로)**

hulk 배포 디렉터리 `.env` 에 다음이 없으면 추가(현재 운영 이미지 태그 유지):
```bash
ssh hulk@192.168.1.111 'cd /home/hulk/working/reporter.crefle.com &&
  grep -q "^REPORTER_TAG=" .env || printf "REGISTRY=hub.crefle.com\nREPORTER_TAG=1.6\nRENDERER_TAG=1.1\n" >> .env &&
  grep -E "^(REGISTRY|REPORTER_TAG|RENDERER_TAG)=" .env'
```
Expected: 세 변수가 출력됨.

- [ ] **Step 2: `.deployed_sha` 초기 마커 시드 (선택 — 첫 실행을 증분으로)**

현재 배포된 커밋을 마커로 심으면 첫 자동 실행이 전체 재빌드 대신 증분으로 동작한다. 현재 운영이
main 최신과 동일하다면:
```bash
ssh hulk@192.168.1.111 'cd /home/hulk/working/reporter.crefle.com && echo "<현재_배포된_main_SHA>" > .deployed_sha && cat .deployed_sha'
```
(생략 시 첫 실행이 baseline 전체 배포로 동작 — 안전하지만 느림.)

- [ ] **Step 3: self-hosted 러너 등록 (사용자: 토큰 발급 필요)**

Task 4 README 의 "러너 설치" 블록 수행. 등록 토큰은 GitHub repo Settings → Actions → Runners →
New self-hosted runner 에서 발급. 완료 후:
```bash
ssh hulk@192.168.1.111 'cd ~/actions-runner && ./run.sh --version 2>/dev/null; systemctl is-active "actions.runner.*" 2>/dev/null || sudo ./svc.sh status'
```
Expected: 러너 서비스 active/online. GitHub repo Settings → Actions → Runners 에 `hulk` 라벨 러너가 Idle 로 표시.

- [ ] **Step 4: 라이브 검증 — 변경 없음 시 skip**

브랜치 머지 후 main 기준으로 GitHub Actions → daily-deploy → Run workflow 수동 실행.
`.deployed_sha` 를 현재 HEAD 로 맞춘 상태라면:
Expected: 로그에 `변경 없음 … skip`, 잡 성공(초록).

- [ ] **Step 5: 라이브 검증 — 콘텐츠 변경 배포**

`proposals/` 에 사소한 변경을 main 에 반영 후 수동 실행.
Expected: 로그에 `계획: … proposals=1 …`, rsync 수행, `docker compose ps` Up healthy 유지,
`.deployed_sha` 가 새 SHA 로 갱신. `http://192.168.1.111:28080` 에 변경 반영.

- [ ] **Step 6: 라이브 검증 — 코드 변경 재빌드/배포 + 헬스**

코드(예: `server.py` 주석) 사소 변경을 main 에 반영 후 수동 실행.
Expected: 뷰어 이미지 `:<short-sha>` 빌드·push, `.env` 태그 갱신, `compose up -d`, `/healthz` 200,
`docker ps` Up healthy, 신규 라우트(`/s/...` 등) 응답. 마커 갱신.

- [ ] **Step 7: (선택) 롤백 동작 검증**

의도적으로 존재하지 않는 태그로 `.env` 를 만들어 헬스 실패를 유발했다가 자동 롤백되는지 확인하거나,
`HEALTH_RETRIES=1` 로 짧게 두고 잘못된 이미지로 한 번 시험. 확인 후 정상 태그로 원복.
Expected: 헬스 실패 시 이전 태그로 `up -d`, 잡 실패(빨강)로 이메일, 마커 미갱신.

---

## Self-Review

**1. Spec coverage** (스펙 각 섹션 → 태스크 매핑):
- §4 아키텍처(self-hosted, 로컬 수행) → Task 3(runs-on) + Task 2(deploy.sh) + Task 5(러너).
- §5 트리거·SHA 게이팅 → Task 3(cron/dispatch) + Task 2(.deployed_sha 비교).
- §6 경로 분류 → Task 2(matches 규칙) + deploy_test.sh 시나리오.
- §7 태그·compose 변수화 → Task 1 + Task 2(set_env/롤백).
- §8 잡 단계(테스트/rsync/빌드/배포/헬스/롤백/마커) → Task 2 전 단계.
- §9 보안(무시크릿·트리거 제한) → Task 3(schedule+dispatch만) + Task 5(러너).
- §10 러너 설치 → Task 4(문서) + Task 5(실행).
- §11 파일 목록 → Task 1~4 각 파일.
- §12 리스크(3.12 컨테이너·COPY 트랩·헬스 롤백) → Task 2(테스트 컨테이너·헬스) + Task 4(COPY 경고).
- §13 검증 → Task 5 Step 4~7.
  → 누락 없음.

**2. Placeholder scan:** 러너 버전 `v2.XYZ`·등록 토큰 `<REG_TOKEN>`·현재 배포 SHA 는 설치 시점 입력값(의도된 런타임 값)으로, 코드/로직 공백이 아님. 그 외 TBD/TODO 없음. 모든 코드 스텝에 완전한 코드 포함.

**3. Type consistency:** deploy.sh 의 함수·변수명(`matches`, `run`, `get_env`, `set_env`, `write_marker`, `healthcheck`, `BUILD_VIEWER/BUILD_RENDERER/SYNC_PROPOSALS/COMPOSE_UP`, `REPORTER_TAG/RENDERER_TAG`)이 compose 변수(Task 1)·워크플로 env(Task 3)·테스트 하네스(Task 2 Step 4) 전반에서 일치. compose 태그 변수 기본값(1.6/1.1)과 .env 시드(Task 1 Step 3, Task 5 Step 1) 일치.
