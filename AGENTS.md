# CREFLE Reports

자체 운영 HTML 문서 열람 서버. FastAPI `server.py` + `proposals/` 자동 색인, hulk에서 Docker로 운영한다.
설치·실행·운영(배포/리포트 갱신) 절차는 `README.md`를 따른다.

## 검증 명령

- 전체 회귀: `.venv/bin/python -m pytest -q`
- 관련 테스트가 있으면 먼저 좁게 실행하고, 완료 전에 전체 회귀를 실행한다.
- 시스템 `pytest` 대신 저장소 가상환경의 Python을 사용한다.

## 하네스: 리포트 등록

**목표:** 외부에서 작성된 HTML 보고서를 검증·배치·동기화하여 reporter 서버(`proposals/` + hulk)에 등록한다.

**트리거:** 새 리포트/보고서 등록·추가·반영·재동기화·버전 갱신 요청 시 `register-report` 스킬을 사용하라.
리포트 작성이나 단순 조회는 직접 응답한다.

**Codex 구성:** `.agents/skills/register-report/` + `.codex/agents/report-registrar.toml`.

## 하네스: 이슈 처리

**목표:** GitHub 이슈 번호를 받아 파악·개발·검증·PR·리뷰까지 5역할(설계자·개발자·테스터·보고자·리뷰어)로 처리한다. 끝점은 리뷰 후 조건부 자동 머지다. `crefle-agent-skills:pr-review` 승인 기준(Blocker·Major 0)과 안전 조건(CI green·충돌 없음)을 충족하면 Squash 머지하고, 미충족이면 PR을 열어둔 채 사람에게 넘긴다.

**트리거:** 이슈를 처리·개발·해결·반영하라는 요청과 그 후속 수정·재개 요청에는 `process-issue` 스킬을 사용하라. 이슈 단순 조회·요약은 직접 응답하고, 완성 HTML 리포트 등록에는 `register-report`를 사용한다.

**Codex 구성:** `.agents/skills/process-issue/` + `.codex/agents/issue-*.toml`.

## 프로젝트 지침

1. 코딩 전에 가정과 완료 조건을 명확히 한다. 여러 해석이 가능하고 결과가 크게 달라지면 사용자에게 묻는다.
2. 요청을 만족하는 최소 변경만 한다. 요청하지 않은 기능·추상화·인접 리팩터링을 추가하지 않는다.
3. 사용자 변경과 무관한 dirty worktree를 보존한다. 모든 변경 라인은 현재 요청에 직접 연결되어야 한다.
4. 새 동작은 재현 테스트를 먼저 추가하고, 관련 테스트와 전체 회귀로 완료를 검증한다.
5. Python 코드 작성·수정에는 `crefle-agent-skills:coding-rules`의 Python 규칙을 적용한다.
6. 커밋·브랜치·PR에는 `crefle-agent-skills:coding-rules`의 Conventional Commits 규칙을 적용한다.

## 하네스 변경 이력

| 날짜 | 변경 내용 | 대상 | 사유 |
|------|----------|------|------|
| 2026-06-17 | 리포트 등록 하네스 구성 | `register-report`, `report-registrar` | 신규 HTML 리포트 등록 자동화 |
| 2026-07-14 | 이슈 처리 하네스 구성 | `process-issue`, 4개 역할 | 이슈→PR 자동화 |
| 2026-07-24 | 리뷰어·팀 표준·조건부 머지 추가 | `process-issue`, `issue-reviewer` | CREFLE 팀 표준 연계 |
| 2026-07-27 | Claude Code 하네스를 Codex 규격으로 포팅 | `AGENTS.md`, `.agents/`, `.codex/` | 두 도구에서 같은 업무 흐름 유지 |
