#!/usr/bin/env bash
# start_issue.sh — 이슈 처리 착수: 이슈 존재 확인 + 최신 origin/main 기준 작업 브랜치 생성.
# 사용: start_issue.sh <issue_number> [--print-only]
#   --print-only : 브랜치를 만들지 않고 이슈 제목·예정 브랜치명만 출력(트리아지 사전 점검용).
# 결정적 착수 절차를 스크립트로 고정해 브랜치 네이밍·베이스(fresh main)를 일관되게 한다.
# 브랜치 네이밍은 팀 표준(crefle-agent-skills coding-rules/commit-convention.md)의
# `<type>/<설명>` 을 따르되, 이슈 추적을 위해 끝에 번호를 붙인다: <type>/<슬러그>-<번호>.
set -Eeuo pipefail

NUM="${1:?issue number required (예: start_issue.sh 6)}"
PRINT_ONLY=0
[ "${2:-}" = "--print-only" ] && PRINT_ONLY=1

# 이슈 존재 확인(없으면 gh 가 비정상 종료 → 여기서 멈춘다)
TITLE="$(gh issue view "$NUM" --json title -q .title)"
STATE="$(gh issue view "$NUM" --json state -q .state)"
LABELS="$(gh issue view "$NUM" --json labels -q '.labels[].name' | tr '\n' ' ')"

# 커밋 type 추론: 팀 라벨(type:*) → 제목 접두사 → GitHub 기본 라벨 → 기본 feat.
TYPE=""
case " $LABELS " in
  *" type:bug "*)     TYPE="fix" ;;
  *" type:feature "*) TYPE="feat" ;;
  *" type:docs "*)    TYPE="docs" ;;
  *" type:task "*)    TYPE="chore" ;;
esac
if [ -z "$TYPE" ]; then
  case "$TITLE" in
    bug:*|fix:*)      TYPE="fix" ;;
    docs:*)           TYPE="docs" ;;
    task:*|chore:*)   TYPE="chore" ;;
    feat:*|feature:*) TYPE="feat" ;;
  esac
fi
if [ -z "$TYPE" ]; then
  case " $LABELS " in
    *" bug "*)           TYPE="fix" ;;
    *" documentation "*) TYPE="docs" ;;
    *" enhancement "*)   TYPE="feat" ;;
  esac
fi
[ -z "$TYPE" ] && TYPE="feat"

# 브랜치 slug: 제목의 `<type>:` 접두사를 뗀 뒤 소문자 ASCII 영숫자만, 나머지는 하이픈, 40자.
# 한글 등은 제거되며 이슈 번호가 식별자 역할을 하므로 비면 'work' 로 폴백.
BARE_TITLE="$(printf '%s' "$TITLE" | sed -E 's/^[a-zA-Z]+[[:space:]]*:[[:space:]]*//')"
SLUG="$(printf '%s' "$BARE_TITLE" | tr '[:upper:]' '[:lower:]' \
  | sed -E 's/[^a-z0-9]+/-/g; s/^-+//; s/-+$//' | cut -c1-40)"
[ -z "$SLUG" ] && SLUG="work"
BRANCH="${TYPE}/${SLUG}-${NUM}"

echo "issue:  #${NUM} [${STATE}] ${TITLE}"
echo "type:   ${TYPE}"
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

# 팀 라벨 체계(issue-management)와 동기화: 착수 → status:in-progress.
# 라벨이 없는 저장소/이슈에서도 착수가 멈추지 않도록 실패는 무시한다.
gh issue edit "$NUM" --add-label "status:in-progress" --remove-label "status:todo" >/dev/null 2>&1 || true
