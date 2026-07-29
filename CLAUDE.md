# CREFLE Reports

자체 운영 HTML 문서 열람 서버. FastAPI `server.py` + `proposals/` 자동 색인, hulk 에서 Docker 운영.
설치·실행·운영(배포/리포트 갱신) 절차는 `README.md` 참조.

## 하네스: 리포트 등록

**목표:** 외부에서 작성된 HTML 보고서를 검증·배치·동기화하여 reporter 서버(`proposals/` + hulk)에 등록한다.

**트리거:** 새 리포트/보고서 등록·추가·반영·재동기화·버전 갱신 요청 시 `register-report` 스킬을 사용하라.
(예: "리포트 등록해줘", "이 html 새로 올려줘", "버전 올려서 다시 반영") 리포트 *작성*이나 단순 조회는 직접 응답.

**변경 이력:**
| 날짜 | 변경 내용 | 대상 | 사유 |
|------|----------|------|------|
| 2026-06-17 | 초기 구성(리포트 등록 하네스) | `agents/report-registrar`, `skills/register-report` | 신규 HTML 리포트 등록 자동화 |

## 하네스: 이슈 처리

**목표:** GitHub 이슈 번호를 받아 파악·개발·검증·PR·리뷰까지 5역할(설계자·개발자·테스터·보고자·리뷰어) 팀으로 처리한다. 끝점은 독립 리뷰 후 열린 PR을 사람에게 인계하는 것이다. pr-review 승인 기준(Blocker·Major 0)과 안전 조건(CI green·충돌 없음)을 판정하되 하네스와 역할 에이전트는 직접 머지하지 않는다.

**트리거:** 이슈를 *처리·개발·해결·반영*하라는 요청 시 `process-issue` 스킬을 사용하라.
(예: "6번 이슈 처리해줘", "이슈 #3 개발해서 PR 올려줘", 그리고 "다시/이어서/수정" 후속) 이슈 *단순 조회·요약*은 직접 응답. 완성 HTML 리포트 등록은 `register-report`.

**변경 이력:**
| 날짜 | 변경 내용 | 대상 | 사유 |
|------|----------|------|------|
| 2026-07-14 | 초기 구성(이슈 처리 하네스) | `agents/issue-{architect,developer,tester,reporter}`, `skills/process-issue` | 이슈→PR 자동화. 설계자(메인/Opus)가 트리아지 후 서브에이전트/팀 선택; 개발자 Opus, 테스터·보고자 Sonnet |
| 2026-07-24 | 팀 표준 스킬(crefle-agent-skills) 연계 — 리뷰어 역할 신설(Opus), 끝점을 PR 생성→리뷰 후 조건부 자동 머지로 확장, 브랜치 `<type>/<슬러그>-<번호>` 전환, 저장소에 type/priority/status 라벨 13종 생성·동기화 | `agents/issue-reviewer`(신규), `agents/issue-{architect,developer,reporter}`, `skills/process-issue`(SKILL.md·start_issue.sh) | coding-rules·issue-management·pr-review 플러그인 도입에 따른 개발팀 하네스 표준화 |
| 2026-07-29 | 이슈 처리 끝점을 리뷰 후 사람 인계로 변경 | `process-issue`, `issue-reviewer` | 자동 머지 권한 제거 |


## 프로젝트 지침

Behavioral guidelines to reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

Tradeoff: These guidelines bias toward caution over speed. For trivial tasks, use judgment.

1. Think Before Coding
Don't assume. Don't hide confusion. Surface tradeoffs.

Before implementing:

State your assumptions explicitly. If uncertain, ask.
If multiple interpretations exist, present them - don't pick silently.
If a simpler approach exists, say so. Push back when warranted.
If something is unclear, stop. Name what's confusing. Ask.
2. Simplicity First
Minimum code that solves the problem. Nothing speculative.

No features beyond what was asked.
No abstractions for single-use code.
No "flexibility" or "configurability" that wasn't requested.
No error handling for impossible scenarios.
If you write 200 lines and it could be 50, rewrite it.
Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

3. Surgical Changes
Touch only what you must. Clean up only your own mess.

When editing existing code:

Don't "improve" adjacent code, comments, or formatting.
Don't refactor things that aren't broken.
Match existing style, even if you'd do it differently.
If you notice unrelated dead code, mention it - don't delete it.
When your changes create orphans:

Remove imports/variables/functions that YOUR changes made unused.
Don't remove pre-existing dead code unless asked.
The test: Every changed line should trace directly to the user's request.

4. Goal-Driven Execution
Define success criteria. Loop until verified.

Transform tasks into verifiable goals:

"Add validation" → "Write tests for invalid inputs, then make them pass"
"Fix the bug" → "Write a test that reproduces it, then make it pass"
"Refactor X" → "Ensure tests pass before and after"
For multi-step tasks, state a brief plan:

1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

These guidelines are working if: fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.
