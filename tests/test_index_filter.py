"""인덱스 검색·유형 필터 마크업 (이슈 #25).

클라이언트 사이드 필터링이므로 JS 동작(부분일치·AND·빈 그룹 숨김 등)은 브라우저/정적
검토로 확인한다. 여기서는 서버가 렌더한 HTML 에 검색 입력·유형 select·건수 id·빈 결과
안내·필터 스크립트가 포함되는지(수용 기준 1·7)를 함수 단위로 검증한다.

render_index() 는 순수 함수라 서버 기동 없이 docs 딕셔너리를 직접 넘겨 호출한다.
"""
from __future__ import annotations

import os

os.environ.setdefault("REPORTS_USER", "reader")
os.environ.setdefault("REPORTS_PASS", "readerpass")
os.environ.setdefault("REPORTS_UPLOAD_USER", "uploader")
os.environ.setdefault("REPORTS_UPLOAD_PASS", "uploaderpass")
os.environ.setdefault("REPORTS_SECRET_KEY", "test-secret-deadbeef-0123456789abcdef")

import server


def _doc(title: str, group: str, rel: str, mtime: float = 1000.0) -> dict:
    """render_index 가 읽는 최소 문서 딕셔너리(_scan_root 산출과 동일한 키)."""
    return {
        "title": title,
        "href": "/" + rel,
        "rel": rel,
        "group": group,
        "mtime": mtime,
        "size_kb": 10,
        "pdf": None,
        "pending_pdf": False,
    }


def _sample_docs() -> list:
    return [
        _doc("삼진엘앤디 신규제안", "proposals", "proposals/samjin/index.html", 3000.0),
        _doc("데모 페이지", "proposals", "proposals/demo/index.html", 2000.0),
        _doc("월간 보고서", "uploads", "uploads/docs/monthly_v1/index.html", 1000.0),
    ]


# ── 수용 기준 1: 검색 입력 + 유형 select 존재 ────────────────────────────────
def test_index_has_search_input():
    html = server.render_index(_sample_docs(), "tester")
    assert 'id="doc-search"' in html


def test_index_has_type_filter_select():
    html = server.render_index(_sample_docs(), "tester")
    assert 'id="doc-type-filter"' in html


# ── 수용 기준 1: select 옵션 = "전체" + 현재 그룹들(값=그룹키, 표시=라벨, 렌더 순서) ──
def test_type_filter_has_all_option_first():
    html = server.render_index(_sample_docs(), "tester")
    sel_start = html.index('id="doc-type-filter"')
    sel_end = html.index("</select>", sel_start)
    options = html[sel_start:sel_end]
    # "전체"(값 없음)가 첫 옵션
    assert '<option value="">전체</option>' in options
    assert options.index('<option value="">전체</option>') < options.index('value="proposals"')


def test_type_filter_options_match_current_groups():
    html = server.render_index(_sample_docs(), "tester")
    sel_start = html.index('id="doc-type-filter"')
    options = html[sel_start:html.index("</select>", sel_start)]
    # 값=그룹키, 표시=_group_label 라벨
    assert f'<option value="proposals">{server._group_label("proposals")}</option>' in options
    assert f'<option value="uploads">{server._group_label("uploads")}</option>' in options


def test_type_filter_preserves_render_order():
    # render_index 는 그룹을 (슬래시 수, 이름)으로 정렬한다 → proposals 가 uploads 앞.
    html = server.render_index(_sample_docs(), "tester")
    sel_start = html.index('id="doc-type-filter"')
    options = html[sel_start:html.index("</select>", sel_start)]
    assert options.index('value="proposals"') < options.index('value="uploads"')


def test_type_filter_only_existing_groups():
    # 문서에 없는 그룹은 옵션에 나오지 않는다(uploads 문서 없음 → uploads 옵션 없음).
    docs = [_doc("제안서 하나", "proposals", "proposals/a/index.html")]
    html = server.render_index(docs, "tester")
    sel_start = html.index('id="doc-type-filter"')
    options = html[sel_start:html.index("</select>", sel_start)]
    assert 'value="proposals"' in options
    assert 'value="uploads"' not in options


# ── 그룹 섹션에 data-group(유형키) 부여 → JS 가 유형별로 섹션을 토글 ──────────
def test_sections_carry_group_key():
    html = server.render_index(_sample_docs(), "tester")
    assert 'data-group="proposals"' in html
    assert 'data-group="uploads"' in html


# ── 수용 기준 5: 0건 안내 문구(빈 화면 금지) ─────────────────────────────────
def test_no_result_notice_element_present():
    html = server.render_index(_sample_docs(), "tester")
    assert 'id="doc-empty"' in html
    assert "일치하는 문서가 없습니다" in html


# ── 수용 기준 6: 헤더 건수에 id 부여(JS 가 표시 중 건수로 갱신) ───────────────
def test_count_has_id_and_total():
    html = server.render_index(_sample_docs(), "tester")
    assert 'id="doc-count"' in html
    assert "3건" in html


# ── 수용 기준 2~4·7: 필터 스크립트 인라인 포함(외부 자산 금지) ───────────────
def test_filter_script_inlined():
    html = server.render_index(_sample_docs(), "tester")
    # 검색·유형 요소를 참조하고 NFC 정규화로 부분일치하는 바닐라 스크립트가 인라인.
    assert "doc-search" in html
    assert "normalize('NFC')" in html
    assert "<script>" in html


# ── 문서 0건이면 필터 바를 렌더하지 않는다(빈 목록 안내만) ────────────────────
def test_filter_bar_absent_when_no_docs():
    html = server.render_index([], "tester")
    assert 'id="doc-search"' not in html
    assert "표시할 문서가 없습니다" in html


# ── 이슈 #27: 건수·빈결과 안내에 라이브 리전 부여(스크린리더 낭독) ────────────
def test_count_has_aria_live_polite():
    # #doc-count 는 aria-live="polite" — 필터 시 갱신되는 건수를 낭독한다.
    html = server.render_index(_sample_docs(), "tester")
    assert 'id="doc-count" aria-live="polite"' in html


def test_empty_notice_has_role_status():
    # #doc-empty 는 role="status"(암묵 polite 라이브 리전) — 결과 없음 안내를 낭독.
    html = server.render_index(_sample_docs(), "tester")
    assert 'id="doc-empty" role="status"' in html


# ── 이슈 #28: 필터 바는 hidden 으로 렌더, JS 초기화가 노출 ────────────────────
def test_filter_bar_hidden_until_js():
    html = server.render_index(_sample_docs(), "tester")
    # JS 미실행 시 필터 바가 화면에 안 나타나도록 hidden 으로 렌더.
    assert '<div class="filters" id="doc-filters" hidden>' in html
    # INDEX_FILTER_JS 는 두 컨트롤을 찾은 뒤 hidden 을 해제한다(정적 확인).
    assert "filters.hidden=false" in html


# ── 이슈 #29: 필터 활성 시 "표시 N / 전체 M건", 해제 시 "M건" ─────────────────
def test_count_badge_active_and_inactive_format():
    html = server.render_index(_sample_docs(), "tester")
    # M(전체)은 초기화 시 카드 수로 계산 — 서버 값 재주입 불필요.
    assert "document.querySelectorAll('li.card').length" in html
    # 활성: "표시 N / 전체 M건" · 비활성: "M건".
    assert "'표시 '+visible+' / 전체 '+total+'건'" in html
    assert "(total+'건')" in html
