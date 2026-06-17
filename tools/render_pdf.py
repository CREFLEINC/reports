#!/usr/bin/env python3
"""
render_pdf.py — HTML 리포트를 PDF로 사전생성한다(Playwright + Chromium).

설계 근거(실측):
- Chromium 만이 JS 구동 덱·최신 CSS 를 충실히 렌더한다(weasyprint/wkhtmltopdf 부적합).
- 덱(deck-stage.js)은 @media print 를 내장해 슬라이드당 1페이지(1920×1080)로 떨어진다.
  → prefer_css_page_size=True 로 그 @page 를 그대로 쓰고, @page 없는 일반 문서는 A4 로 떨어진다.
- 인터랙티브 데모는 재생/스크롤로 콘텐츠가 생성된다 → 캡처 전 자동 트리거(스크롤+재생 클릭+
  애니메이션 즉시 종료)로 펼친 뒤 PDF 화.

사용:
    render_pdf.py <html파일> [출력.pdf]      # 한 건
    render_pdf.py --all [--force]             # proposals/ 전체(누락/오래된 것만; --force 면 전부)
    공통 옵션: --interactive auto|on|off (기본 auto = 데모 자동 감지)

산출물: 기본적으로 <html>.pdf (같은 폴더). 정적 서버가 그대로 제공한다.
의존성(빌드/등록 머신 전용, 운영 컨테이너 아님): playwright, 그리고 `playwright install chromium`.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright

REPO = Path(__file__).resolve().parent.parent
DOCS_DIR = REPO / "proposals"


def _rel(p: Path) -> str:
    try:
        return str(p.relative_to(REPO))
    except ValueError:
        return str(p)

# 인터랙티브 데모 자동 감지용 텍스트(보이는 텍스트에 하나라도 있으면 demo 모드).
# 일반 문서 오탐을 막기 위해 느슨한 "▶" 대신 재생 버튼 고유 문구만 사용. (파일명 '데모'도 감지)
INTERACTIVE_HINTS = ["시연 재생", "재생 버튼", "데모 재생", "시나리오 ② 재생"]
# 캡처 전 클릭할 재생/시연 트리거 후보 텍스트
PLAY_TEXTS = ["시연 재생", "재생", "▶", "시작", "데모 재생", "자동 재생", "play", "Play", "PLAY"]

ANIM_OFF_CSS = (
    "*{animation-duration:0s!important;animation-delay:0s!important;"
    "transition-duration:0s!important;transition-delay:0s!important;"
    "scroll-behavior:auto!important;}"
)

AUTOSCROLL_JS = """
async () => {
  await new Promise((res) => {
    let y = 0;
    const step = () => {
      window.scrollTo(0, y);
      y += Math.max(300, window.innerHeight * 0.8);
      if (y < document.documentElement.scrollHeight) setTimeout(step, 50);
      else { window.scrollTo(0, 0); res(); }
    };
    step();
  });
}
"""

# 재생/시연 트리거를 텍스트·aria-label·title·class 로 폭넓게 찾아 클릭한다.
CLICK_TRIGGERS_JS = """
(texts) => {
  const hit = [];
  const sel = 'button,[role=button],a,[onclick],.btn,[class*=play],[class*=Play],[class*=cta],[data-play]';
  const match = (s) => s && texts.some((x) => s.includes(x));
  for (const el of Array.from(document.querySelectorAll(sel))) {
    const t = (el.innerText || el.textContent || '').trim();
    const a = (el.getAttribute('aria-label') || '') + ' ' + (el.getAttribute('title') || '');
    if (match(t) || match(a) || el.hasAttribute('data-play')) {
      try { el.click(); hit.push((t || a).slice(0, 30)); } catch (e) {}
    }
  }
  return hit;
}
"""


def is_interactive_name(html: Path) -> bool:
    name = html.name
    return ("데모" in name) or ("demo" in name.lower())


async def _settle_fonts(page) -> None:
    try:
        await page.wait_for_function(
            "document.fonts && document.fonts.status==='loaded'", timeout=4000
        )
    except Exception:
        pass


async def render_one(browser, html: Path, pdf: Path, interactive: str) -> None:
    page = await browser.new_page(viewport={"width": 1440, "height": 900})
    await page.goto(html.as_uri(), wait_until="networkidle", timeout=90000)
    await _settle_fonts(page)
    await page.wait_for_timeout(1500)  # deck-stage data-fonts-pending reveal 등 정착

    demo = interactive == "on" or (interactive == "auto" and await _looks_interactive(page, html))
    if demo:
        await page.add_style_tag(content=ANIM_OFF_CSS)
        await page.evaluate(AUTOSCROLL_JS)
        for _ in range(2):
            clicked = await page.evaluate(CLICK_TRIGGERS_JS, PLAY_TEXTS)
            if clicked:
                print(f"    · 트리거 클릭: {clicked}")
                await page.wait_for_timeout(1200)
        await page.wait_for_timeout(7000)   # 재생 시퀀스 완료 대기
        await page.evaluate(AUTOSCROLL_JS)  # 새로 생성된 콘텐츠 반영
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        await page.wait_for_timeout(500)

    pdf.parent.mkdir(parents=True, exist_ok=True)
    await page.pdf(
        path=str(pdf),
        print_background=True,
        prefer_css_page_size=True,  # 덱의 @page(1920×1080) 우선, 없으면 A4
        format="A4",
        margin={"top": "0", "bottom": "0", "left": "0", "right": "0"},
    )
    await page.close()
    print(f"  ✓ {_rel(html)} → {_rel(pdf)} ({pdf.stat().st_size // 1024}KB){' [demo]' if demo else ''}")


async def _looks_interactive(page, html: Path) -> bool:
    if is_interactive_name(html):
        return True
    try:
        body = (await page.inner_text("body"))[:20000]
    except Exception:
        return False
    return any(h in body for h in INTERACTIVE_HINTS)


def discover() -> list:
    out = []
    for p in sorted(DOCS_DIR.rglob("*.html")):
        if p.name.lower() == "index.html":
            continue
        if any(part.startswith(".") for part in p.relative_to(DOCS_DIR).parts):
            continue
        out.append(p)
    return out


def needs_render(html: Path, pdf: Path, force: bool) -> bool:
    if force or not pdf.exists():
        return True
    return pdf.stat().st_mtime < html.stat().st_mtime  # PDF 가 HTML 보다 오래되면 재생성


async def main() -> int:
    ap = argparse.ArgumentParser(description="HTML 리포트 → PDF 사전생성")
    ap.add_argument("html", nargs="?", help="단일 HTML 경로")
    ap.add_argument("out", nargs="?", help="출력 PDF 경로(생략 시 <html>.pdf)")
    ap.add_argument("--all", action="store_true", help="proposals/ 전체 렌더")
    ap.add_argument("--force", action="store_true", help="최신이어도 재생성")
    ap.add_argument("--interactive", choices=["auto", "on", "off"], default="auto")
    args = ap.parse_args()

    if args.all:
        targets = [(h, h.with_suffix(".pdf")) for h in discover()]
        targets = [(h, pd) for (h, pd) in targets if needs_render(h, pd, args.force)]
        if not targets:
            print("최신 상태 — 생성할 PDF 없음.")
            return 0
    elif args.html:
        h = Path(args.html).resolve()
        if not h.is_file():
            print(f"❌ 파일 없음: {h}", file=sys.stderr)
            return 2
        pd = Path(args.out).resolve() if args.out else h.with_suffix(".pdf")
        targets = [(h, pd)]
    else:
        ap.print_help()
        return 2

    print(f"렌더 대상 {len(targets)}건 (interactive={args.interactive})")
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        for h, pd in targets:
            try:
                await render_one(browser, h, pd, args.interactive)
            except Exception as e:
                print(f"  ❌ {h.name}: {e}", file=sys.stderr)
        await browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
