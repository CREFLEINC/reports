---
name: process-issue
description: GitHub 이슈 번호를 받아 요구 분석, 구현, 검증, PR 생성, 독립 리뷰, 조건부 Squash 머지까지 수행하는 CREFLE Reports 전용 5역할 워크플로. "이슈 N번 처리/개발/해결/반영", "PR까지/머지까지", 이전 이슈 작업을 "이어서/다시/수정/보완"하라는 요청에 반드시 사용한다. 이슈 단순 조회·요약에는 사용하지 않고, 완성된 HTML 리포트 등록에는 register-report를 사용한다.
---

# GitHub 이슈 처리

메인 에이전트가 설계자이자 오케스트레이터를 맡고, 저장소의 Codex 사용자 정의 에이전트를 순서대로 호출한다.

- 개발: `issue-developer`
- 검증: `issue-tester`
- PR: `issue-reporter`
- 리뷰·머지 판정: `issue-reviewer`

역할 설정은 `.codex/agents/*.toml`, 감사 가능한 중간 산출물은 `_workspace/issue-<n>/`에 둔다.

## 시작 전에 읽을 것

1. 이 파일 전체를 읽는다.
2. 업무 지시서를 만들 때 `references/work-order-contract.md`를 읽는다.
3. 검증을 맡길 때 `references/verification-playbook.md`를 읽는다.
4. 코드 작성에는 `crefle-agent-skills:coding-rules`, 라벨에는 `crefle-agent-skills:issue-management`, 리뷰에는 `crefle-agent-skills:pr-review`를 사용한다. 플러그인이 없으면 일반 기준으로 진행하되 자동 머지는 금지한다.

## 안전 원칙

- 사용자의 "이슈 처리" 요청은 이 워크플로의 커밋·push·PR·조건부 머지를 승인한 것으로 본다. 이슈 번호가 없으면 요청한다.
- 승인 기준이나 GitHub 안전 상태를 하나라도 확신할 수 없으면 머지하지 않는다.
- 현재 작업 트리의 기존 변경을 보존한다. 착수 전에 변경 파일과 기존 `_workspace`를 확인하고, 이슈와 무관한 변경은 커밋에 넣지 않는다.
- 에이전트는 같은 작업 디렉터리를 공유한다. 쓰기 작업을 병렬화할 때 파일 소유 범위를 겹치지 않게 지정하고, 다른 에이전트의 변경을 되돌리지 말라고 명시한다.
- 최대 동시 슬롯을 초과하지 않는다. 기본은 단계별 순차 실행이며, 복잡한 이슈의 독립 구현 조각만 최대 3개까지 병렬 실행한다.

## Phase 0: 이어서 할 작업 판별

`_workspace/issue-<n>/`와 현재 브랜치·PR을 확인한다.

- 산출물 없음: 초기 실행한다.
- 산출물 있음 + 부분 수정/재개: 완료된 Phase를 반복하지 말고 해당 역할부터 재개한다.
- 기존 PR 있음: 새 PR을 만들지 말고 같은 PR을 갱신한다.
- 사용자 요구가 기존 계획과 충돌: 기존 산출물을 삭제하지 말고 차이를 기록한 뒤 계획을 갱신한다.

## Phase 1: 설계와 착수

1. `.agents/skills/process-issue/scripts/start_issue.sh <n>`을 실행하여 이슈와 최신 `origin/main` 기준 브랜치를 준비하고 `status:in-progress`를 반영한다.
2. 이슈 본문·코멘트·연결 PR과 관련 코드를 읽는다.
3. 목표, 범위, 수용 기준, 대상 파일, 비목표, 검증 방법을 확정한다. 모호해서 결과가 달라질 수 있으면 구현 전에 사용자에게 묻는다.
4. 실행 모드를 결정한다.
   - 사소한 문서·설정 변경: 메인에서 구현하고 테스터만 호출할 수 있다.
   - 1~2개 파일의 국소 변경: 개발자 1명을 호출한다.
   - 3개 이상 파일 또는 독립 결과물: 겹치지 않는 파일 소유권으로 최대 3개 구현 조각을 나눈다.
5. `references/work-order-contract.md` 형식으로 `_workspace/issue-<n>/01_architect_plan.md`를 작성한다.

## Phase 2: 구현

`spawn_agent`로 `agent_type: issue-developer`를 호출한다. 프롬프트에는 이슈 번호, 업무 지시서, 소유 파일, 비목표, 산출물 경로를 포함한다.

각 개발자에게 다음을 명시한다.

- 코드 작성 전 `crefle-agent-skills:coding-rules`와 해당 언어 참조를 읽을 것
- 다른 에이전트도 같은 작업 트리를 사용하므로 타인의 변경을 되돌리지 않을 것
- 담당 파일 밖 변경이 필요하면 먼저 메인에 알릴 것
- 관련 테스트를 실행하고 `_workspace/issue-<n>/02_dev_<slice>_report.md`에 결과를 기록할 것

독립 조각은 병렬 실행할 수 있다. 파일이 겹치면 순차 실행한다. 완료 후 결과 메시지와 실제 diff를 모두 확인한다.

## Phase 3: 검증

구현 조각마다 `agent_type: issue-tester`를 호출한다. 테스터에게 수용 기준, 설계서, 개발 보고, 실제 diff, 판정서 경로를 전달한다.

테스터는 다음을 수행한다.

1. 수용 기준별 PASS/FAIL 대조
2. 관련 테스트 실행
3. 필요한 실제 라우트·스크립트 동작 확인
4. 전체 `.venv/bin/python -m pytest -q` 회귀 실행
5. `_workspace/issue-<n>/03_tester_<slice>_verdict.md` 작성

FAIL이면 `followup_task`로 원 개발자에게 구체적인 결함을 돌려보내고, 수정 후 같은 테스터에게 재검증을 요청한다. 동일 결함이 3회 반복되면 사용자 판단을 요청한다.

## Phase 4: 커밋과 PR

모든 판정이 PASS일 때만 `agent_type: issue-reporter`를 호출한다. 다음을 맡긴다.

- 이슈에 속한 변경만 선별하여 Conventional Commits 형식으로 커밋
- push 후 `main` 기준 PR 생성 또는 기존 PR 갱신
- PR 본문에 `Closes #<n>`, 변경 이유, 구현 요약, 검증 증거, 비목표 포함
- 이슈 라벨을 `status:in-review`로 변경
- `_workspace/issue-<n>/04_reporter_pr.md` 작성

메인 에이전트는 PR URL, base/head, 포함 파일, 테스트 근거가 계획과 일치하는지 확인한다.

## Phase 5: 독립 리뷰와 조건부 머지

`agent_type: issue-reviewer`를 호출하고 PR 번호, 이슈 번호, 계획·검증 산출물 경로를 전달한다.

리뷰어는 `crefle-agent-skills:pr-review`와 `crefle-agent-skills:coding-rules`를 실제로 읽고 다음을 수행한다.

1. PR 본문과 전체 diff 확인
2. 정확성·보안·테스트·컨벤션 4영역 검토
3. Blocker/Major/Minor/Nit 분류와 한국어 리뷰 코멘트 게시
4. `_workspace/issue-<n>/05_reviewer_verdict.md` 작성
5. Blocker와 Major가 0이고, base=`main`, Draft 아님, `mergeable=MERGEABLE`, `mergeStateStatus=CLEAN`일 때만 `gh pr merge --squash --delete-branch`
6. 머지 성공 시 이슈 라벨을 `status:done`으로 변경

Blocker/Major가 있으면 한 번만 개발→검증→PR 갱신→델타 리뷰 루프를 실행한다. 다시 실패하거나 CI·충돌·권한 상태가 불확실하면 PR을 열어둔 채 보류한다.

## 협업 도구 사용

- 새 역할 시작: `spawn_agent`
- 실행 중 에이전트에 정보 전달: `send_message`
- 완료된 동일 역할에 수정·재검증 요청: `followup_task`
- 상태 확인: `list_agents`
- 결과 대기: `wait_agent`

에이전트가 실패하면 동일 역할을 한 번 재시도한다. 재실패하면 누락과 영향 범위를 기록하고, 필수 단계라면 진행을 중단한다.

## 완료 보고

사용자에게 다음만 간결하게 보고한다.

- 이슈와 구현 결과
- 테스트 결과
- PR URL
- 리뷰 판정
- 머지했다면 머지 커밋, 보류했다면 정확한 이유

## 테스트 시나리오

- 정상: 이슈 처리 요청 → 설계 → 개발 → 테스트 PASS → PR → 리뷰 승인 및 CI green → Squash 머지
- 검증 실패: 테스터 FAIL → 원 개발자 수정 → 재검증 → 이후 단계 진행
- 리뷰 반려: Major 발견 → 한 차례 수정·재검증·델타 리뷰 → 통과 시 머지, 재실패 시 보류
- 안전 조건 미충족: CI pending 또는 충돌 → 리뷰는 게시하되 머지하지 않음
