#!/usr/bin/env python3
"""Render banner.html → PNG at 2x via Playwright/Chromium (clip to .banner)."""
import asyncio, sys
from pathlib import Path
from playwright.async_api import async_playwright

HTML = Path("/tmp/crefle-banner/banner.html")
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/crefle-banner/out.png")


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(
            viewport={"width": 1320, "height": 360}, device_scale_factor=2
        )
        await page.goto(HTML.as_uri(), wait_until="networkidle", timeout=60000)
        # let webfonts (Spoqa + Material Symbols + JetBrains Mono) settle
        try:
            await page.wait_for_function(
                "document.fonts && document.fonts.status==='loaded'", timeout=6000
            )
        except Exception:
            pass
        await page.wait_for_timeout(600)
        el = await page.query_selector("#cap")
        await el.screenshot(path=str(OUT), omit_background=True)
        await browser.close()
        kb = OUT.stat().st_size // 1024
        print(f"✓ {OUT}  ({kb} KB)")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
