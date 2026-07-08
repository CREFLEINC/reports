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
