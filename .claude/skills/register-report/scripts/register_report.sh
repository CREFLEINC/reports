#!/usr/bin/env bash
# ============================================================================
# register_report.sh — 신규 HTML 리포트 등록(결정적 부분)
#
#   reports 프로젝트의 proposals/ 적절 위치에 HTML 을 배치하고,
#   (내부망 가정) hulk 로 rsync 동기화하여 reporter 서버에 즉시 반영한다.
#   server.py 가 매 요청마다 proposals/ 를 스캔하므로 재시작/재빌드 불필요.
#
# 사용:
#   register_report.sh --src <file.html> --type <type> --name <name> --version <ver> [옵션]
#
# 필수:
#   --src <path>       등록할 HTML 파일 경로(외부 프로젝트 산출물)
#   --type <type>      문서 유형: proposal | demo | ohmyfactory | <기타>
#   --name <name>      리포트 이름(파일명에 사용, 공백/한글 허용)
#   --version <ver>    버전(예: 1, 2, 0.1 — 앞의 v 는 자동 제거)
#
# 선택:
#   --assets <dir>     리포트가 참조하는 상대 자산 디렉터리(내용을 대상 폴더로 병합)
#   --no-sync          hulk 동기화 생략(배치만)
#   --commit           배치 후 git add+commit (기본: 미커밋)
#   --push             커밋 후 git push (--commit 동반)
#   --force            대상 파일이 이미 있어도 덮어쓰기
#   --hulk <u@h:dir>   hulk proposals 경로(기본 아래 HULK_DEST)
#   --base-url <url>   반영 확인용 베이스 URL(기본 아래 BASE_URL)
#
# 환경변수: REPORTS_USER/REPORTS_PASS (반영 확인 Basic Auth, 기본 crefle/crefle)
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/../../../.." && pwd)"   # scripts→register-report→skills→.claude→REPO

HULK_DEST="hulk@192.168.1.111:/home/hulk/working/reporter.crefle.com/proposals/"
BASE_URL="http://192.168.1.111:28080"
U="${REPORTS_USER:-crefle}"; P="${REPORTS_PASS:-crefle}"

SRC=""; TYPE=""; NAME=""; VER=""; ASSETS=""
DO_SYNC=1; DO_COMMIT=0; DO_PUSH=0; FORCE=0

die(){ echo "❌ $*" >&2; exit 1; }
note(){ echo "• $*"; }

# ---- 인자 파싱 -------------------------------------------------------------
while [ $# -gt 0 ]; do
  case "$1" in
    --src) SRC="${2:-}"; shift 2;;
    --type) TYPE="${2:-}"; shift 2;;
    --name) NAME="${2:-}"; shift 2;;
    --version) VER="${2:-}"; shift 2;;
    --assets) ASSETS="${2:-}"; shift 2;;
    --hulk) HULK_DEST="${2:-}"; shift 2;;
    --base-url) BASE_URL="${2:-}"; shift 2;;
    --no-sync) DO_SYNC=0; shift;;
    --commit) DO_COMMIT=1; shift;;
    --push) DO_PUSH=1; DO_COMMIT=1; shift;;
    --force) FORCE=1; shift;;
    -h|--help) sed -n '2,30p' "$0"; exit 0;;
    *) die "알 수 없는 인자: $1";;
  esac
done

[ -n "$SRC" ]  || die "--src 필요"
[ -n "$TYPE" ] || die "--type 필요"
[ -n "$NAME" ] || die "--name 필요"
[ -n "$VER" ]  || die "--version 필요"
[ -f "$SRC" ]  || die "src 파일이 없음: $SRC"
case "$SRC" in *.html|*.htm) ;; *) die "src 는 .html 파일이어야 함: $SRC";; esac

# ---- 유형 → 대상 폴더 ------------------------------------------------------
case "$TYPE" in
  proposal|proposals|demo) REL_DIR="proposals";;
  ohmyfactory)             REL_DIR="proposals/ohmyfactory";;
  *)                       REL_DIR="proposals/$TYPE";;   # 신규 유형은 동명 하위 폴더
esac
TARGET_DIR="$REPO/$REL_DIR"

# ---- 파일명: {name}_v{version}.html ---------------------------------------
VER="${VER#v}"; VER="${VER#V}"                          # 앞의 v/V 제거
SAFE_NAME="$(printf '%s' "$NAME" | tr ' ' '_' | tr -d '/\\:*?"<>|')"
FNAME="${SAFE_NAME}_v${VER}.html"
DEST="$TARGET_DIR/$FNAME"
REL_PATH="$REL_DIR/$FNAME"

echo "── 등록 계획 ────────────────────────────────"
note "원본 : $SRC"
note "유형 : $TYPE  →  $REL_DIR/"
note "대상 : $REL_PATH"
[ -n "$ASSETS" ] && note "자산 : $ASSETS → $REL_DIR/"
echo "─────────────────────────────────────────────"

if [ -e "$DEST" ] && [ "$FORCE" -ne 1 ]; then
  die "이미 존재: $REL_PATH (덮어쓰려면 --force, 또는 --version 을 올리세요)"
fi

# ---- 배치 -----------------------------------------------------------------
mkdir -p "$TARGET_DIR"
cp -f "$SRC" "$DEST"
note "배치 완료: $REL_PATH"
if [ -n "$ASSETS" ]; then
  [ -d "$ASSETS" ] || die "--assets 디렉터리가 없음: $ASSETS"
  cp -R "$ASSETS"/. "$TARGET_DIR"/
  note "자산 병합 완료: $REL_DIR/"
fi

# ---- HTML 점검(제목 + 상대 자산 존재) — python3 위임, 경고만(등록은 계속) ----
CHECK="$(python3 - "$DEST" "$TARGET_DIR" <<'PY'
import sys, re, html, os
dest, tdir = sys.argv[1], sys.argv[2]
data = open(dest, encoding="utf-8", errors="replace").read()
m = re.search(r"<title[^>]*>(.*?)</title>", data, re.I | re.S)
print("TITLE\t" + (html.unescape(re.sub(r"\s+"," ",m.group(1)).strip()) if m else ""))
seen=set()
for r in re.findall(r"(?:src|href)\s*=\s*[\"']([^\"']+)[\"']", data, re.I):
    r0=r.split("#")[0].split("?")[0]
    if not r0 or r0 in seen: continue
    seen.add(r0); low=r0.lower()
    if low.startswith(("http://","https://","//","data:","mailto:","tel:")): continue
    if r0.startswith("/"): print("ABS\t"+r0); continue
    if not os.path.exists(os.path.normpath(os.path.join(tdir,r0))): print("MISSING\t"+r0)
PY
)"
TITLE="$(printf '%s\n' "$CHECK" | awk -F'\t' '$1=="TITLE"{print $2}')"
[ -n "$TITLE" ] && note "제목(목차 라벨): $TITLE" || echo "⚠️  <title> 없음 → 목차에 파일명으로 표시됨"
if printf '%s\n' "$CHECK" | grep -q '^MISSING'; then
  echo "⚠️  대상 폴더에 없는 상대 자산 참조:"
  printf '%s\n' "$CHECK" | awk -F'\t' '$1=="MISSING"{print "     - "$2}'
  echo "     → --assets 로 자산을 함께 넣거나, 해당 자산이 대상 폴더에 있어야 정상 렌더됩니다."
fi
if printf '%s\n' "$CHECK" | grep -q '^ABS'; then
  echo "⚠️  루트 절대경로(/...) 참조 발견 — 동적 서버에서 깨질 수 있음:"
  printf '%s\n' "$CHECK" | awk -F'\t' '$1=="ABS"{print "     - "$2}'
fi

# ---- git 커밋(선택) -------------------------------------------------------
if [ "$DO_COMMIT" -eq 1 ]; then
  ( cd "$REPO"
    git add "$REL_PATH" $([ -n "$ASSETS" ] && echo "$REL_DIR")
    git commit -q -m "Add report: $NAME v$VER ($TYPE)" \
      -m "등록 경로: $REL_PATH" \
      -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>" \
    && note "git commit 완료"
    [ "$DO_PUSH" -eq 1 ] && git push -q && note "git push 완료" || true
  )
fi

# ---- hulk 동기화(내부망 가정) ---------------------------------------------
SYNCED=0
if [ "$DO_SYNC" -eq 1 ]; then
  HOSTSPEC="${HULK_DEST%%:*}"   # hulk@192.168.1.111
  if ssh -o BatchMode=yes -o ConnectTimeout=5 "$HOSTSPEC" true 2>/dev/null; then
    rsync -az --delete -e "ssh -o BatchMode=yes" "$REPO/proposals/" "$HULK_DEST"
    SYNCED=1
    note "hulk 동기화 완료: $HULK_DEST"
  else
    echo "⚠️  hulk($HOSTSPEC) 에 접속할 수 없습니다(내부망이 아니거나 키 미설정)."
    echo "     파일은 repo 에 배치되었습니다. 내부망에서 아래로 동기화하세요:"
    echo "     rsync -az --delete proposals/ $HULK_DEST"
  fi
else
  note "동기화 생략(--no-sync)"
fi

# ---- 반영 확인 ------------------------------------------------------------
if [ "$SYNCED" -eq 1 ]; then
  ENC="$(python3 -c "import sys,urllib.parse; print(urllib.parse.quote(sys.argv[1]))" "$REL_PATH")"
  CODE="$(curl --path-as-is -s -o /dev/null -w '%{http_code}' -u "$U:$P" "$BASE_URL/$ENC" || echo 000)"
  if [ "$CODE" = "200" ]; then
    note "반영 확인: $BASE_URL/$ENC → 200 ✅"
  else
    echo "⚠️  반영 확인 실패: $BASE_URL/$ENC → $CODE (서버/인증 상태를 확인하세요)"
  fi
fi

echo ""
echo "✅ 등록 처리 완료"
echo "   - repo 경로 : $REL_PATH"
[ "$SYNCED" -eq 1 ] && echo "   - 열람 URL  : $BASE_URL/$(python3 -c "import sys,urllib.parse; print(urllib.parse.quote(sys.argv[1]))" "$REL_PATH")"
echo "   - 목차      : $BASE_URL/"
