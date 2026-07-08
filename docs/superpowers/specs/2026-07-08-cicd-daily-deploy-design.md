# 설계: 매일 17:00 KST 조건부 자동 배포 (CI/CD)

- 날짜: 2026-07-08
- 상태: 승인됨 (구현 대기)
- 대상: `github.com/CREFLEINC/reports` → hulk(192.168.1.111) Docker Compose 운영

## 1. 배경 / 동기

reporter 문서 서버는 hulk에서 Docker Compose로 운영되며, 현재 배포는 **전부 수동**이다:
로컬(arm64)에서 `buildx` 크로스빌드 → Harbor push → hulk에서 `docker compose pull && up -d`,
리포트 갱신은 `rsync -az --delete proposals/`. 이를 **매일 17:00 KST에 main의 변경 사항이 있을 때만
자동으로 운영에 반영**하도록 자동화한다.

## 2. 목표 / 비목표

**목표**
- 매일 17:00 KST에 실행되어, 지난 배포 이후 main이 바뀐 경우에만 운영에 반영.
- 반영 범위: `proposals/` 콘텐츠 동기화 + 코드 변경 시 이미지 재빌드·Harbor push·재배포.
- 배포 전 pytest green 게이트, 배포 후 헬스체크 + 실패 시 자동 롤백.
- 실패 알림은 GitHub 기본 이메일(Actions 실패 통지).

**비목표 (YAGNI)**
- 도메인/TLS·리버스프록시, watchtower류 pull 배포, Slack/Discord 알림, staging 등 멀티환경.
- push/PR 트리거 즉시 배포(의도적으로 하루 1회 배치 배포만).

## 3. 핵심 제약 (설계를 규정하는 사실)

- hulk(192.168.1.111)와 Harbor(hub.crefle.com)는 **사설 LAN 전용** → GitHub 클라우드 러너로 접근 불가.
  따라서 실제 빌드·배포는 **hulk 내부 self-hosted 러너**에서 로컬로 수행한다.
- hulk = x86_64, Ubuntu 20.04(**시스템 Python 3.8**), Docker 28.0.1, Compose v2.33.1, Harbor 로그인 상주.
  server.py는 3.12 타깃 → **테스트는 `python:3.12-slim` 컨테이너**에서 실행(운영 동일 환경).
- 배포 디렉터리 `/home/hulk/working/reporter.crefle.com/`: `docker-compose.yml`·`.env`·`proposals/`는
  `hulk` 소유(러너=hulk 유저가 로컬 쓰기 가능), `uploads/`는 별도 소유·git/rsync 미러 아님(건드리지 않음).

## 4. 아키텍처

```
GitHub Actions (schedule 0 8 * * * UTC = 17:00 KST) + workflow_dispatch
        │  오케스트레이션·스케줄·게이팅만 (시크릿 없음)
        ▼  runs-on: [self-hosted, hulk]
hulk 내부 self-hosted runner (systemd 상시) ── 전 과정 로컬 수행
        ├─ checkout → 변경 판정(.deployed_sha vs HEAD)
        ├─ pytest (python:3.12-slim 컨테이너) — 실패 시 중단
        ├─ rsync proposals/ → 배포디렉터리 (로컬, 무중단)
        ├─ 조건부 docker build (native amd64) → Harbor push (:<short-sha>)
        ├─ .env 태그 갱신 + docker compose pull && up -d
        └─ 헬스체크(/healthz) → 실패 시 이전 태그로 롤백 → 성공 시 .deployed_sha 기록
```

GitHub은 "언제 돌릴지"만 정하고 실제 작업은 hulk 안에서 로컬로 일어난다. SSH·터널·외부 노출 없음.

## 5. 트리거 & "변경 있으면" 게이팅

- 트리거: `schedule: '0 8 * * *'`(UTC = 17:00 KST) + `workflow_dispatch`(수동/롤백/테스트).
  GitHub cron은 수 분~수십 분 지연 가능하나 허용됨(사용자 확인).
- 변경 판정: 배포 디렉터리의 **`.deployed_sha` 마커** vs 현재 main HEAD(`GITHUB_SHA`).
  - 같으면 → 변경 없음 → skip(exit 0).
  - 다르면 → 배포 진행. `git diff --name-only <deployed_sha>..<HEAD>`로 변경 경로 분류.
  - 마커 없음(최초) → 전체 배포(두 이미지 빌드 + rsync)로 베이스라인 확립.
- 성공 시에만 `.deployed_sha ← HEAD` 기록 → 멱등·누락일 자동 보정.

## 6. 변경 경로 분류

| 분류 | 트리거 경로 | 동작 |
|------|-------------|------|
| 뷰어 이미지 재빌드 | `server.py`, `uploads_handler.py`, `shares.py`, `requirements.txt`, `Dockerfile` | reporter 재빌드+push+재배포 |
| 렌더러 이미지 재빌드 | `Dockerfile.renderer`, `tools/render_pdf.py`, `renderer/worker.py` | renderer 재빌드+push+재배포 |
| 배포 설정 변경 | `docker-compose.yml` | 재빌드 없이 `compose up -d` |
| 콘텐츠 동기화 | `proposals/**` | rsync만(무중단) |
| 무시 | `uploads/**`, `docs/**`, `tests/**`, `README.md`, `.github/**`, `.env*` | 배포 동작 없음 |

- `proposals/`가 diff에 있으면 항상 rsync. 코드/렌더러 변경 시에만 해당 이미지 재빌드.
- 이미지·compose 변경이 하나라도 있으면 `compose up -d` 수행(콘텐츠만이면 rsync로 끝).

## 7. 이미지 태깅 & compose 변경

현재 compose는 태그 하드코딩(`:1.6`,`:1.1`)이라 자동화 불가 → **환경변수 태그**로 변경:

```yaml
# docker-compose.yml
image: ${REGISTRY:-hub.crefle.com}/service/reporter:${REPORTER_TAG:-1.6}
image: ${REGISTRY:-hub.crefle.com}/service/reporter-renderer:${RENDERER_TAG:-1.1}
```

- 빌드는 **immutable `:<short-sha>`**(`git rev-parse --short HEAD`)로 push.
- 배포 디렉터리 `.env`의 `REPORTER_TAG`/`RENDERER_TAG`를 그 SHA로 **upsert** 후 `compose pull && up -d`.
  compose는 프로젝트 디렉터리 `.env`를 변수 치환에 사용하므로 태그가 반영됨.
- 롤백 = 태그 변수를 직전 값으로 되돌리고 `up -d`. 감사·재현성 유지.
- 기본값 폴백(`:-1.6`, `:-1.1`)이 있어 **기존 수동 절차도 그대로 동작**.

## 8. 잡 단계 (deploy.sh 로직)

워크플로는 얇게(checkout + `bash ops/deploy.sh`), 배포 로직은 **`ops/deploy.sh`**에 둔다(로컬 테스트 가능).
`ops/deploy.sh`는 다음 환경을 받는다: `GITHUB_SHA`, `GITHUB_WORKSPACE`, `DEPLOY_DIR`(기본
`/home/hulk/working/reporter.crefle.com`), `REGISTRY`(기본 `hub.crefle.com`).

1. **변경 판정**: `OLD=$(cat $DEPLOY_DIR/.deployed_sha 2>/dev/null)`. `OLD == HEAD`면 로그 남기고 exit 0.
2. **경로 분류**: `OLD` 있으면 `git diff --name-only $OLD..HEAD`, 없으면 전체(full) 플래그.
3. **테스트 게이트**:
   `docker run --rm -e PYTHONDONTWRITEBYTECODE=1 -v "$GITHUB_WORKSPACE":/w -w /w python:3.12-slim
   bash -c "pip install -q -r requirements.txt -r requirements-dev.txt && pytest -q"`.
   실패 시 마커 미갱신 후 exit 1(→ GitHub 실패 메일).
4. **콘텐츠 동기화**: proposals 변경/full 이면
   `rsync -az --delete "$GITHUB_WORKSPACE/proposals/" "$DEPLOY_DIR/proposals/"`.
5. **조건부 빌드+push**:
   - 뷰어: `docker build -t $REGISTRY/service/reporter:$SHORT .` → `docker push …:$SHORT`.
   - 렌더러: `docker build -f Dockerfile.renderer -t $REGISTRY/service/reporter-renderer:$SHORT .` → push.
6. **배포**: 이미지/compose 변경 시 `cp docker-compose.yml "$DEPLOY_DIR/"`, `.env` 태그 upsert
   (직전 값 `PREV_*` 보관), `cd $DEPLOY_DIR && docker compose pull && docker compose up -d`.
7. **헬스체크**: `/healthz` 200을 최대 N회(예: 12회 × 5s) 폴링 + `docker compose ps`가 Up이면 성공.
   실패 시 `.env` 태그를 `PREV_*`로 되돌리고 `compose up -d`(롤백) 후 exit 1.
8. **마커 기록**: 성공 시 `echo $HEAD > $DEPLOY_DIR/.deployed_sha`.

## 9. 시크릿 & 보안

- **GitHub 시크릿 불필요**: 러너가 hulk 로컬(Harbor 로그인 상주, `.env`는 서버 상주, SSH 없음).
- 배포 워크플로 트리거는 **schedule + workflow_dispatch 뿐**(push·PR 없음) →
  포크/PR의 신뢰불가 코드가 self-hosted 러너에서 실행될 위험 원천 차단. (repo도 private)
- 러너는 **systemd 서비스**로 상시 기동(17:00 온라인 보장, 재부팅 자동 복구).

## 10. 러너 설치 (hulk, 1회)

```bash
# hulk@192.168.1.111
mkdir -p ~/actions-runner && cd ~/actions-runner
curl -o actions-runner-linux-x64.tar.gz -L \
  https://github.com/actions/runner/releases/download/vX.Y.Z/actions-runner-linux-x64-X.Y.Z.tar.gz
tar xzf actions-runner-linux-x64.tar.gz
./config.sh --url https://github.com/CREFLEINC/reports \
  --token <REPO_SETTINGS_ACTIONS_RUNNERS_에서_발급> --labels hulk --unattended
sudo ./svc.sh install hulk && sudo ./svc.sh start   # systemd 상시화
```

> 등록 토큰은 GitHub repo Settings → Actions → Runners → New self-hosted runner 에서 발급(사용자 수행).
> 러너는 hulk 유저로 실행되어 docker/Harbor/배포 디렉터리에 로컬 접근.

## 11. 신규/변경 파일

| 파일 | 변경 |
|------|------|
| `.github/workflows/deploy.yml` | 신규 — schedule + workflow_dispatch, `runs-on: [self-hosted, hulk]`, checkout + deploy.sh 호출, concurrency 가드 |
| `ops/deploy.sh` | 신규 — 판정·테스트·빌드·배포·헬스·롤백 로직(멱등) |
| `docker-compose.yml` | 이미지 태그 env 변수화(`REGISTRY`/`REPORTER_TAG`/`RENDERER_TAG`) |
| `.env.example` | `REGISTRY`·`REPORTER_TAG`·`RENDERER_TAG` 추가(문서화) |
| hulk `.env` | 위 3개 변수 추가(1회, 현 이미지 태그 1.6/1.1로 시드) |
| `README.md` | "CI/CD 자동 배포" 섹션 + 러너 설치·롤백·COPY 트랩 경고 |

## 12. 리스크 & 완화

- **GitHub schedule 지연/60일 비활성화**: 지연 허용(확인됨). repo 활성 상태라 비활성화 무관. 문서화.
- **hulk Python 3.8 ≠ 3.12**: 테스트를 3.12 컨테이너에서 실행해 회피(설계 반영).
- **Dockerfile COPY 누락 트랩**(과거 shares.py 프로덕션 크래시): 헬스체크+자동 롤백이 안전망.
  새 최상위 모듈 추가 시 `COPY` 갱신 필요를 README에 경고.
- **러너 오프라인**: systemd 상시화. 꺼져도 SHA 게이팅이라 다음 실행에서 누락분 자동 반영.
- **첫 실행 폭주 방지**: 최초 마커 없음 → full 배포 1회. 이후 증분.

## 13. 검증 (구현 후)

- `workflow_dispatch`로 수동 실행 → 변경 없음일 때 skip, 코드/콘텐츠 변경 시 각 경로 동작 확인.
- 배포 후 `docker compose ps`(Up healthy)·`/healthz` 200·신규 라우트 응답 확인.
- 헬스 실패를 의도적으로 유발(잘못된 태그)해 자동 롤백 동작 확인.
- `.deployed_sha`가 성공 시에만 갱신되는지 확인.
