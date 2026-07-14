#!/usr/bin/env bash
# start_issue.sh — 이슈 처리 착수: 이슈 존재 확인 + 최신 origin/main 기준 작업 브랜치 생성.
# 사용: start_issue.sh <issue_number> [--print-only]
#   --print-only : 브랜치를 만들지 않고 이슈 제목·예정 브랜치명만 출력(트리아지 사전 점검용).
# 결정적 착수 절차를 스크립트로 고정해 브랜치 네이밍·베이스(fresh main)를 일관되게 한다.
set -Eeuo pipefail

NUM="${1:?issue number required (예: start_issue.sh 6)}"
PRINT_ONLY=0
[ "${2:-}" = "--print-only" ] && PRINT_ONLY=1

# 이슈 존재 확인(없으면 gh 가 비정상 종료 → 여기서 멈춘다)
TITLE="$(gh issue view "$NUM" --json title -q .title)"
STATE="$(gh issue view "$NUM" --json state -q .state)"

# 브랜치 slug: 소문자 ASCII 영숫자만, 나머지는 하이픈, 40자. 한글 등은 제거되며
# 이슈 번호가 식별자 역할을 하므로 비면 'work' 로 폴백.
SLUG="$(printf '%s' "$TITLE" | tr '[:upper:]' '[:lower:]' \
  | sed -E 's/[^a-z0-9]+/-/g; s/^-+//; s/-+$//' | cut -c1-40)"
[ -z "$SLUG" ] && SLUG="work"
BRANCH="issue/${NUM}-${SLUG}"

echo "issue:  #${NUM} [${STATE}] ${TITLE}"
echo "branch: ${BRANCH}"

if [ "$PRINT_ONLY" = "1" ]; then
  exit 0
fi

git fetch origin --quiet
if git switch -c "$BRANCH" origin/main 2>/dev/null; then
  echo "created: ${BRANCH} (base: origin/main)"
else
  git switch "$BRANCH"
  echo "reused: ${BRANCH} (already exists)"
fi
