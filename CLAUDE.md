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
