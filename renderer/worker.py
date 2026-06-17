#!/usr/bin/env python3
"""
renderer/worker.py — 업로드 문서 PDF 생성 워커(격리 컨테이너 전용).

공유 볼륨(uploads/)의 파일시스템 큐(uploads/queue/*.json)를 폴링하여, 각 작업을 원자적으로
클레임하고 tools/render_pdf.py 의 render_one() 으로 PDF 를 생성한다. 이 컨테이너만 Chromium 을
포함하며 network_mode:none 으로 격리된다(file:// 렌더라 네트워크 불필요).

설계 포인트
  - 원자적 클레임: os.rename(<id>.json → <id>.inprogress) — 동시/다중 워커 안전.
  - 문서마다 fresh 브라우저: render_pdf.py --all 의 단일 브라우저 재사용(상태 누수)을 회피.
  - wall-clock kill: asyncio.wait_for(timeout) — 무한 스크롤/행 페이지가 큐를 막지 않게.
  - 원자적 출력: tmp 에 쓴 뒤 os.replace 로 index.pdf 교체(반쪽 PDF 미노출).
  - 재시도/회수: 실패 시 attempts++ 재큐(최대 3), 크래시 잔여 .inprogress 는 재기동 시 회수.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

# render_pdf.py 위치 탐색: 컨테이너(같은 폴더로 COPY) + 로컬 repo(tools/) 모두 지원.
_here = Path(__file__).resolve().parent
for _cand in (_here, _here.parent / "tools", _here.parent):
    if (_cand / "render_pdf.py").is_file():
        sys.path.insert(0, str(_cand))
        break
from render_pdf import render_one  # noqa: E402

from playwright.async_api import async_playwright  # noqa: E402

UPLOADS_DIR = Path(os.environ.get("REPORTS_UPLOADS_DIR", "/work/uploads")).resolve()
QUEUE = UPLOADS_DIR / "queue"
DONE = QUEUE / "done"
TMP = UPLOADS_DIR / "tmp"
TIMEOUT = int(os.environ.get("RENDER_TIMEOUT_SEC", "120"))
POLL = float(os.environ.get("RENDER_POLL_SEC", "1.5"))
MAX_ATTEMPTS = int(os.environ.get("RENDER_MAX_ATTEMPTS", "3"))


def _log(msg: str) -> None:
    print(f"[renderer] {msg}", flush=True)


def _ensure_dirs() -> None:
    for d in (QUEUE, DONE, TMP):
        d.mkdir(parents=True, exist_ok=True)


def _requeue(jid: str, data: dict) -> None:
    tmp = QUEUE / f".{jid}.json.tmp"
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, QUEUE / f"{jid}.json")


def _finish(jid: str, status: str, data: dict, note: str = "") -> None:
    data = {**data, "status": status, "note": note, "finished": time.time()}
    (DONE / f"{jid}.{status}").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _reclaim_stale() -> None:
    """크래시로 남은 .inprogress(2*TIMEOUT 초과)를 재큐."""
    cutoff = time.time() - 2 * TIMEOUT
    for p in QUEUE.glob("*.inprogress"):
        try:
            if p.stat().st_mtime < cutoff:
                jid = p.name[: -len(".inprogress")]
                data = json.loads(p.read_text(encoding="utf-8"))
                os.replace(p, QUEUE / f"{jid}.json")
                _log(f"회수(재큐): {jid}")
        except (OSError, ValueError):
            pass


async def _render(html_path: Path, pdf_out: Path) -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch()  # 작업마다 fresh 브라우저
        try:
            await asyncio.wait_for(render_one(browser, html_path, pdf_out, "auto"), timeout=TIMEOUT)
        finally:
            await browser.close()


async def _process(job_file: Path) -> None:
    jid = job_file.stem  # "<jid>.json" → "<jid>"
    claim = QUEUE / f"{jid}.inprogress"
    try:
        os.rename(job_file, claim)  # 원자적 클레임
    except OSError:
        return  # 다른 워커가 가져갔거나 사라짐
    try:
        data = json.loads(claim.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        _finish(jid, "fail", {"jid": jid}, f"손상된 작업: {e}")
        claim.unlink(missing_ok=True)
        return

    doc = (UPLOADS_DIR / data.get("rel", "")).resolve()
    html_path = doc / data.get("html", "index.html")
    attempts = int(data.get("attempts", 0)) + 1
    data["attempts"] = attempts

    if not html_path.is_file():
        _finish(jid, "fail", data, "문서(index.html) 없음")
        claim.unlink(missing_ok=True)
        return

    TMP.mkdir(parents=True, exist_ok=True)
    tmp_pdf = TMP / f"{jid}.pdf"
    try:
        _log(f"렌더 시작: {data.get('rel')} (시도 {attempts})")
        await _render(html_path, tmp_pdf)
        os.replace(tmp_pdf, doc / "index.pdf")  # 원자적 사이드카 교체
        _finish(jid, "ok", data)
        claim.unlink(missing_ok=True)
        _log(f"완료: {data.get('rel')}")
    except Exception as e:  # noqa: BLE001
        tmp_pdf.unlink(missing_ok=True)
        if attempts < MAX_ATTEMPTS:
            _requeue(jid, data)
            claim.unlink(missing_ok=True)
            _log(f"실패→재큐({attempts}/{MAX_ATTEMPTS}): {data.get('rel')} · {e}")
        else:
            _finish(jid, "fail", data, str(e))
            claim.unlink(missing_ok=True)
            _log(f"실패(포기): {data.get('rel')} · {e}")


async def _drain_once() -> int:
    """현재 큐의 작업을 모두 처리하고 처리 건수를 반환(--once / 수동 백필용)."""
    _ensure_dirs()
    _reclaim_stale()
    n = 0
    for job in sorted(QUEUE.glob("*.json")):
        await _process(job)
        n += 1
    return n


async def main() -> None:
    _ensure_dirs()
    _log(f"시작 · 큐={QUEUE} · timeout={TIMEOUT}s · poll={POLL}s")
    last_reclaim = 0.0
    while True:
        now = time.time()
        if now - last_reclaim > TIMEOUT:
            _reclaim_stale()
            last_reclaim = now
        jobs = sorted(QUEUE.glob("*.json"))
        if not jobs:
            await asyncio.sleep(POLL)
            continue
        for job in jobs:
            await _process(job)


if __name__ == "__main__":
    if "--once" in sys.argv or os.environ.get("RENDER_ONCE") == "1":
        count = asyncio.run(_drain_once())
        _log(f"--once 완료: {count}건 처리")
    else:
        asyncio.run(main())
