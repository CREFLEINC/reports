# README 헤더 배너 — 소스

`../reports-header.png` (README 상단 헤더 이미지)의 재생성 소스다.

- `banner.html` — CREFLE 디자인 시스템(다크 인디고 그라디언트 + 40px 그리드 + 브랜드 마크 + Spoqa Han Sans Neo)으로 구성한 1280×320 배너. 모티프는 이 서버의 동작 그대로 — Basic Auth 게이트 → `proposals/` 자동 색인 TOC → 문서별 ⬇ PDF → 렌더러가 만드는 "PDF 생성 중…" 상태.
- `render.py` — Playwright/Chromium 으로 `banner.html` 의 `.banner` 영역을 2배 해상도 PNG(2560×640)로 캡처.

## 재생성
```bash
.venv/bin/python assets/header-src/render.py assets/reports-header.png
```

> **의존성**: `banner.html` 의 `@font-face` 가 `crefle_designer` 스킬의 OTF 폰트를
> 절대경로(`~/.claude/skills/crefle_designer/fonts/*.otf`)로 참조한다. 그 스킬이 설치된
> 머신에서만 폰트가 정확히 렌더된다(없으면 Noto Sans KR 로 폴백). 브랜드 토큰의 단일
> 출처는 `crefle_designer` 스킬이다.
