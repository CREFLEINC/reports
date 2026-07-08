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
