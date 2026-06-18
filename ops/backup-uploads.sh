#!/usr/bin/env bash
# ============================================================================
# backup-uploads.sh — uploads/ 일일 백업 → hulk 2nd disk(/home/storage_disk2)
#
# uploads/ 는 git·rsync 미러가 아닌 서버 볼륨 전용 소스 오브 트루스라 별도 백업이 필요하다.
# tar 스냅샷 + 보존 회전. hulk 의 cron 으로 매일 실행한다.
#   crontab 예: 0 3 * * * /home/hulk/working/reporter.crefle.com/backup-uploads.sh >> /home/storage_disk2/reporter-backup/backup.log 2>&1
#
# 참고: uploads/ 는 컨테이너(uid 1001)가 쓰지만 파일이 644/디렉터리 755 라 host(hulk uid 1000)가
#       읽어 tar 할 수 있다. 혹시 권한으로 막히면 루트 컨테이너로 tar 하도록 전환할 것.
# ============================================================================
set -euo pipefail

SRC_DIR="${REPORTS_DIR:-/home/hulk/working/reporter.crefle.com}"
DEST_DIR="${BACKUP_DIR:-/home/storage_disk2/reporter-backup}"
KEEP="${BACKUP_KEEP:-14}"   # 최근 N개 보관

[ -d "$SRC_DIR/uploads" ] || { echo "$(date '+%F %T') ERROR: $SRC_DIR/uploads 없음" >&2; exit 1; }
mkdir -p "$DEST_DIR"

ts="$(date +%Y%m%d_%H%M)"
out="$DEST_DIR/uploads-$ts.tar.gz"
tar czf "$out" -C "$SRC_DIR" uploads

# 보존 회전: 최신 KEEP 개만 유지
ls -1t "$DEST_DIR"/uploads-*.tar.gz 2>/dev/null | tail -n +"$((KEEP + 1))" | xargs -r rm -f

echo "$(date '+%F %T') backup ok: $out ($(du -h "$out" | cut -f1)) · 보관 $(ls -1 "$DEST_DIR"/uploads-*.tar.gz 2>/dev/null | wc -l | tr -d ' ')개"
