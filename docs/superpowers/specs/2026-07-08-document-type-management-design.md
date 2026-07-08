# 설계: 문서 유형 관리 기능 (이슈 #6)

- 날짜: 2026-07-08
- 상태: 승인 대기
- 이슈: #6 "신규 문서 유형 추가 기능"

## 1. 배경

업로드 문서 유형이 `server.py`의 `render_upload_form()`에 **하드코딩**(`("proposal","demo","ohmyfactory")`)돼
있어 유형을 추가·변경·삭제할 수 없다. 유형 구분이 없는 문서를 위한 "기타" 유형도 없다. 유형을 uploader가
관리할 수 있게 만든다.

현재 구조:
- 유형 = `uploads/docs/<슬러그>/` 폴더. 업로드는 `<슬러그>/<이름>_v<버전>/index.html`로 저장.
- 인덱스 그룹 라벨: `_group_label()`이 `uploads/<슬러그>`를 "업로드 · <슬러그>"로 표시(커스텀 라벨 없음).
- 슬러그 검증: `uploads_handler._TYPE_RE = ^[a-z0-9][a-z0-9_-]{0,30}$`.
- 영속 패턴 선례: `shares.py` + `uploads/shares.json`(원자적 tmp→replace, `threading.Lock`, 테스트 격리).

## 2. 목표 / 비목표

**목표**
- uploader가 문서 유형을 **추가 / 이름변경 / 삭제**한다.
- 내장 **"기타"(etc)** 유형 — 삭제·이름변경 불가한 fallback.
- 예외: 이미 존재하는 슬러그 추가 → 알림(409). 유형 삭제 → 그 유형 문서를 **기타로 일괄 이동**.
- 업로드 드롭다운·인덱스 그룹 라벨이 유형 레지스트리를 반영.

**비목표**
- git 큐레이션 `proposals/` 쪽 그룹(`GROUP_LABELS`)은 이 기능 범위 밖(코드+rsync 관리 유지).
- 슬러그(폴더 키) 변경(폴더 이동·URL 변경 유발) — 이번엔 라벨만 변경.

## 3. 결정 사항 (확정)

- **유형 추가 입력**: 표시 이름(한글) + 영문 슬러그. 슬러그는 이름에서 자동 제안하되 수정 가능.
- **관리 UI**: 전용 페이지 `/types`(uploader 전용), 업로드 페이지에서 링크 진입.
- **이름 변경 = 라벨만**(슬러그/폴더 불변). **기타는 삭제·변경 불가**.
- **삭제 = 레지스트리 제거 + 문서 폴더를 기타로 이동 + 활성 공개링크 재지정**.

## 4. 데이터 모델 · 저장 (`types.py` + `uploads/types.json`)

`shares.py` 패턴을 그대로 따르는 순수 모듈. server를 import하지 않고 환경변수로 독립 설정.
`uploads/types.json`에 영속(원자적 tmp→`os.replace`, `threading.Lock`). 테스트는 모듈 전역
`types.TYPES_FILE` 를 임시 파일로 덮어써 격리(`REPORTS_TYPES_FILE` env도 지원).

스키마(JSON, 순서 보존 배열):
```json
[
  {"slug": "proposal", "label": "제안서",       "builtin": false},
  {"slug": "demo",     "label": "데모",         "builtin": false},
  {"slug": "etc",      "label": "기타",         "builtin": true}
]
```

**시드(파일 없을 때 최초 생성)**: 기존 하드코딩 3종(`proposal→제안서`, `demo→데모`,
`ohmyfactory→OhMyFactory`) + `기타(etc)` + `uploads/docs/` 아래 실재하나 목록에 없는 슬러그(라벨=슬러그).
→ 기존 업로드 문서가 유형에 정상 소속되도록.

**불변식**: `etc`(기타)는 load 시 항상 존재하도록 보장. 슬러그 유일.

## 5. 함수 (`types.py`)

- `load_types() -> list[dict]` — 없으면 시드; `etc` 보장; 순서 반환.
- `add_type(slug, label) -> dict` — 슬러그 검증(`_TYPE_RE` 재사용) + 라벨 정리; **중복이면 `ValueError`**.
- `rename_type(slug, new_label) -> dict` — 라벨 변경; 미존재/`etc`면 `ValueError`.
- `delete_type(slug) -> None` — 레지스트리에서 제거; 미존재/`etc`면 `ValueError`. (문서 이동은 server가 수행.)
- `type_exists(slug) -> bool`, `label_for(slug) -> str | None`.
- 라벨 정리: NFC, strip, 제어문자 제거, 1~40자(초과 시 오류).

## 6. API (uploader 전용, catch-all 앞 등록)

- `GET /api/types` → `[{slug,label,builtin,count}]`. `count` = `uploads/docs/<slug>/` 하위 문서 수(삭제 확인용).
- `POST /api/types` `{slug,label}` → 201; **중복 409** "이미 존재하는 유형입니다".
- `PATCH /api/types/{slug}` `{label}` → 200; `etc`/미존재 422/404.
- `DELETE /api/types/{slug}` → 200 `{moved:N}`; `etc` 400. 문서 이동 수행.

검증 오류는 `HTTPException`(기존 패턴). 미인증은 `require_uploader` 의존성.

## 7. 삭제 시 문서 이동 (server가 조율)

1. `uploads/docs/<slug>/` 하위 각 문서 디렉터리를 `uploads/docs/etc/`로 이동. 이름 충돌 시 `<name>_2`,
   `<name>_3`… 으로 회피.
2. 활성 공개 링크 재지정: `shares.py`에 `rebase_doc_paths(old_prefix, new_prefix)` 헬퍼 추가 —
   `doc_rel`/`doc_dir`가 `docs/<slug>/`로 시작하는 레코드를 `docs/etc/`로 재작성(링크 유지).
3. 비게 된 `uploads/docs/<slug>/` 폴더 제거. 그 후 `types.delete_type(slug)`.

경로 안전: 이동 대상은 반드시 `uploads/docs/` 하위여야 함(`_within` 재사용).

## 8. 통합 지점

- `render_upload_form()`: 하드코딩 튜플 → `types.load_types()`로 `<option value="slug">label</option>`.
  헤더에 "유형 관리(`/types`)" 링크 추가.
- `_group_label(g)`: `uploads/<slug>` → `types.label_for(slug)` 사용(없으면 기존 fallback 유지).
- `render_types_page()`: 신규 관리 페이지(목록 + 추가폼 + 행별 이름변경/삭제, `etc`는 비활성). 소규모 JS로
  `/api/types` 호출. 슬러그 자동 제안(이름의 ASCII 소문자화, 비-ASCII면 빈칸 → 사용자 입력).
- (선택) `uploads_handler`: 업로드 시 미등록 슬러그 거부는 하지 않음(유연성). 형식 검증만 유지 —
  단, 업로드 폼은 등록된 유형만 노출하므로 실사용상 등록 유형으로 수렴.

## 9. 테스트 (`tests/test_types.py`, httpx TestClient)

- 레지스트리: 시드, `etc` 보장, add/rename/delete, 순서.
- 예외: 중복 추가(ValueError/409), `etc` 삭제·변경 차단, 삭제 시 문서 이동(+이름 충돌), 공유 재지정.
- API: 401(uploader 아님), 409(중복), PATCH/DELETE 결과, `count` 정확.
- 통합: 업로드 폼이 레지스트리 반영(라벨 노출), 인덱스 그룹 라벨.
- 테스트 격리: `types.TYPES_FILE`·`shares.SHARES_FILE`·`UPLOADS_DOCS`를 tmp로 지정.

## 10. 신규/변경 파일

| 파일 | 변경 |
|------|------|
| `types.py` | 신규 — 레지스트리 순수 모듈 |
| `tests/test_types.py` | 신규 — 단위·API 테스트 |
| `server.py` | `/api/types*` 라우트, `/types` 페이지, 업로드 폼·`_group_label` 통합, 삭제 조율 |
| `shares.py` | `rebase_doc_paths()` 헬퍼(삭제 이동 시 링크 재지정) |
| `Dockerfile` | **`COPY` 줄에 `types.py` 추가** (누락 시 컨테이너 import 크래시 — 과거 shares.py 트랩) |
| `README.md` | "문서 유형 관리" 섹션 |

## 11. 리스크 & 완화

- **Dockerfile COPY 누락**: 새 최상위 모듈 `types.py`를 반드시 COPY에 추가(헬스체크가 잡지만 원인 예방).
- **삭제 중 크래시**: 문서 이동 → 공유 재지정 → 레지스트리 제거 순서. 이동을 먼저 끝내고 마지막에 레지스트리
  제거하므로 중단 시에도 "이동됐지만 유형은 남음" 상태(재시도로 수렴). 데이터 유실 없음.
- **동시성**: 레지스트리 쓰기는 `_LOCK`. 문서 이동은 uploader 단독 조작 전제(낮은 동시성).
- **기존 shares 경로**: 삭제 이동 시에만 rebase. 그 외 유형은 무영향.

## 12. 검증

- pytest 전체 green(신규 test_types 포함).
- 로컬 앱 기동 후: `/types`에서 추가→업로드 드롭다운 반영→인덱스 라벨, 중복 추가 알림, 삭제 시 문서가
  기타 그룹으로 이동, 활성 공개링크 유지 확인.
