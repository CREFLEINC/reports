# 업로드 ZIP — macOS 메타데이터 자동 정리 + 단일 래핑 폴더 평탄화

- 날짜: 2026-06-23
- 대상: `uploads_handler.py` (웹 업로드 self-service, M1)
- 상태: 설계 승인됨 → 구현

## 문제 (근본 원인)

`/upload` 으로 macOS Finder 가 만든 zip 을 올리면 메타데이터 때문에 실패하거나 게시물이
오염된다. `_extract_zip_safe()` 는 zip 멤버마다 확장자 화이트리스트(`ALLOWED_EXT`)를 강제한다:

1. **`.DS_Store`** → `Path(".DS_Store").suffix == ""`(빈 확장자) → `허용되지 않는 확장자: (없음)`
   으로 **전체 업로드가 422 거부**된다. (사용자가 겪는 "오류".)
2. **`__MACOSX/._index.html`, `._style.css`** (AppleDouble) → 확장자가 `.html`/`.css` 라
   검증을 통과 → 게시 디렉터리에 `__MACOSX/` 정크가 남는다. top-level 에 `._foo.html` 이
   있으면 `_resolve_doc_html()` 의 "단일 .html" 판정이 2개로 깨져 또 실패한다.
3. **Finder '폴더 압축'** 은 내용물을 단일 폴더로 감싼 zip 을 만든다 → top-level 에
   `index.html` 이 없어 `_resolve_doc_html()` 이 실패한다.

## 목표

macOS(또는 Windows) 탐색기로 만든 zip 이 정크 메타데이터나 단일 래핑 폴더 때문에 거부되지
않고, 깔끔한 게시물로 처리되도록 한다. 기존 보안 보장(zip-slip / zip-bomb / symlink /
확장자 / traversal)은 모두 유지한다.

## 설계

변경은 전부 `uploads_handler.py` 한 파일 + 신규 테스트 파일 + README 한 단락.
서버 라우팅·인증·렌더러 큐는 손대지 않는다.

### 1. 정크 판별 헬퍼 (신규)

```python
_JUNK_BASENAMES = {"thumbs.db", "desktop.ini"}   # 소문자 비교

def _is_junk_member(name: str) -> bool:
    """탐색기 메타데이터 zip 멤버인지. __MACOSX 트리 / dot-시작 / Windows 정크."""
    parts = Path(name).parts
    if "__MACOSX" in parts:
        return True
    base = parts[-1] if parts else name
    return base.startswith(".") or base.lower() in _JUNK_BASENAMES
```

- `.DS_Store`, `._*`(AppleDouble), `.fseventsd` 등 dot-시작 전부 + `__MACOSX/` 트리 +
  Windows `Thumbs.db`/`desktop.ini` 포괄. server.py `_scan_root` 의 dot-경로 무시와 일관.

### 2. `_extract_zip_safe()` 루프에 정크 스킵 삽입

순서가 핵심 — 보안 검사는 그대로 두고, 정크 스킵은 **경로-traversal 검사 *뒤*, 확장자
검사 *앞*** 에 넣는다:

| 단계 | 동작 |
|---|---|
| 1 | 디렉터리 엔트리(`/` 로 끝남) → continue *(기존)* |
| 2 | null·절대경로·`\`·`:` → 거부 *(기존)* |
| 3 | `..` traversal → 거부 *(기존)* |
| **4** | **`_is_junk_member(nm)` → continue (신규)** |
| 5~ | 심볼릭링크·확장자 화이트리스트·크기·zip-slip·추출 *(기존)* |

- 정크는 디스크에 아예 안 써진다(공격면 축소). 악의적 `__MACOSX/../../evil` 는 3단계에서
  여전히 거부된다(보안 회귀 방지).
- 실제 파일을 하나도 추출하지 못한 zip(전부 정크)이면 추출 후
  `_bad("zip 에 게시할 콘텐츠가 없습니다(메타데이터만 포함).")` 로 명확히 실패한다.
  (`wrote_any` 플래그로 판정.)

### 3. 단일 래핑 폴더 평탄화 헬퍼 (신규)

```python
def _flatten_single_root(stage: Path) -> None:
    """top-level 이 단일 디렉터리뿐이고 top-level 에 index.html 이 없으면 한 단계 끌어올림."""
```

- 추출(정크 제거 완료) 후 `_resolve_doc_html()` 직전 호출.
- top-level 에 디렉터리 하나만 있고 파일이 없는 동안 **반복**(매 반복 중첩 깊이 1 감소 → 항상
  종료, `max_depth=64` 는 비정상 입력용 러너웨이 가드) → `a/b/index.html` 같은 다중·깊은
  래핑도 처리. top-level 에 파일(html 등)이 있으면 평탄화하지 않으므로 정상 구조는 절대
  건드리지 않는다.
- 충돌 안전 절차: 단일 inner 디렉터리를 **`.lift-<uuid>`**(dot+uuid) 로 rename → stage 가
  비워짐 → inner 의 자식들을 빈 stage 로 rename → holding rmdir. uuid 라 `<name>__lift`
  같은 자식이 안에 있어도 이름 충돌이 불가능하다.

### 4. `handle_upload()` 호출 순서

```python
_extract_zip_safe(raw, stage)
raw.unlink(missing_ok=True)
_flatten_single_root(stage)   # 신규
_resolve_doc_html(stage)
```

### 5. 테스트 (TDD, 신규 `tests/test_uploads.py`)

`_extract_zip_safe`/`_flatten_single_root` 는 경로 인자를 받으므로 `tmp_path` 로 직접 단위
테스트(비동기 불필요):

- `.DS_Store` 포함 zip → 더 이상 거부 안 됨 + 디스크에 `.DS_Store` 없음
- `__MACOSX/._*`, `Thumbs.db`, `desktop.ini` 스킵 확인
- Finder 폴더압축(`mydoc/index.html` + `mydoc/assets/*` + `__MACOSX/*`) → 평탄화 후
  `stage/index.html` 존재
- 전부-정크 zip → 422 + 명확 메시지
- `__MACOSX/../../evil.html` → **여전히 422**(보안 회귀 가드)
- e2e 1건: TestClient `POST /upload`(uploader 쿠키)로 Finder zip → 201, 게시 디렉터리에
  `__MACOSX` 없음 (dest·queue 정리)

### 6. 문서

README "웹 업로드" 단락에 "macOS Finder zip(폴더 압축·`__MACOSX`/`.DS_Store`)은 자동
정리됨" 한 줄 추가.

## 손대지 않는 것 / 메모

- `MAX_ENTRIES`(2000)는 정크 엔트리도 그대로 카운트한다. Finder zip 은 파일당 `._` 그림자로
  엔트리가 2배가 되니 수백 파일 초대형 문서에선 한도에 닿을 수 있으나, 현실적으로 드물다.
  이번 범위에선 보수적으로 유지한다.
- `.html` 단일 파일 업로드 경로는 zip 추출을 거치지 않으므로 영향 없음.

## 검토 반영 / 알려진 한계 (적대적 리뷰 후)

3-렌즈 적대적 리뷰(보안·정확성·회귀)에서 5건 제기 → 4건 확정. 반영 결과:

- **(수정, medium)** `_flatten_single_root` 가 `<name>__lift` 이름의 자식과 충돌해 `OSError`
  → HTTP 500 을 내던 버그. holding 이름을 `.lift-<uuid>` 로 바꿔 충돌 불가하게 수정.
- **(수정, low)** 단일 폴더가 10단계보다 깊게 중첩되면 평탄화가 끊겨 오해성 422. 가드를 64 로
  올리고 "구조 안정까지 반복" 으로 명확화(항상 종료).
- **(수용, low)** 최상위 래핑 폴더 이름이 `.` 으로 시작하면(`.report/...`) 전체가 "메타데이터
  만" 으로 거부된다. server 가 dot-경로를 색인/제공하지 않는 정책과 일관 → 안전한 422 로 수용.
- **(수용, low)** dot-디렉터리 내부의 정상 콘텐츠(`.well-known/` 등)는 조용히 드롭된다. 의도된
  동작이며 HTML 리포트 서버 용도엔 무관 → 수용. (필요 시 추후 audit 로깅으로 가시화 가능.)
- **(거부)** "빈 단일 래핑 폴더 → 오해성 메시지" 는 `wrote_any` 가드가 추출 단계에서 먼저
  거부하므로 실제 업로드 경로에서 도달 불가 → 무효.
