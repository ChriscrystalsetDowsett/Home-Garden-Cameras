#!/bin/bash
# Nightly: move this camera's photos and videos to bigPC (same script on every Pi).
#
#   ~/sync-to-bigpc.sh            normal run (cron)
#   ~/sync-to-bigpc.sh --dry-run  show what would move
#
# - Sends to bigPC's wired LAN address (Tailscale keys expire; the LAN doesn't).
# - Only files older than 10 minutes, and never timelapse frames in progress
#   (tl_*), raw recordings (*.mjpeg / *.wav) or ffmpeg list files. Those are
#   still being written, and the app deletes them itself when it is done.
# - Thumbnails stay on the Pi so the gallery keeps its history.
# - rsync deletes a Pi copy only after it arrived intact (--remove-source-files).
# - Writes ~/.sync-status.json (last run, last success, files moved) for the
#   health check, and keeps the last 300 lines of ~/sync-to-bigpc.log.
set -u
BIGPC=chris@192.168.4.138
PI=$(hostname -s); [ "$PI" = chris ] && PI=pi3          # pi3's hostname is "chris"
APP=/home/chris/dev/apps/home-garden-cameras/data
DEST=/mnt/storage/media/$PI
[ "$PI" = pi4 ] && DEST=/mnt/storage/media/pi4/home-camera   # pi4/photos is the greenhouse archive
LOG=$HOME/sync-to-bigpc.log
STATUS=$HOME/.sync-status.json
DRY=""; [ "${1:-}" = "--dry-run" ] && DRY="--dry-run"
SSH="ssh -o BatchMode=yes -o ConnectTimeout=15"

log() { echo "$(date '+%F %T') $*" >> "$LOG"; }
rc=0; moved=0
log "=== sync start -> $BIGPC:$DEST $DRY"
if ! $SSH "$BIGPC" "mkdir -p '$DEST/photos' '$DEST/videos' '$DEST/logs'" 2>>"$LOG"; then
    log "bigPC unreachable"; rc=255
else
    for kind in photos videos; do
        src="$APP/$kind"; [ -d "$src" ] || continue
        list=$(mktemp)
        (cd "$src" && find . -maxdepth 1 -type f -mmin +10 \
            ! -name '*.thumb.jpg' ! -name 'tl_*' ! -name '*.mjpeg' ! -name '*.wav' \
            ! -name '_tl_list_*' ! -name '*.tmp' ! -name '.*' -printf '%P\n') > "$list"
        n=$(wc -l < "$list")
        if [ "$n" -gt 0 ]; then
            if rsync -a $DRY --files-from="$list" --remove-source-files -e "$SSH" \
                    "$src/" "$BIGPC:$DEST/$kind/" >> "$LOG" 2>&1; then
                log "$kind: $n file(s) moved"; moved=$((moved + n))
            else
                r=$?; log "$kind: rsync FAILED rc=$r"; rc=$r
            fi
        else
            log "$kind: nothing to move"
        fi
        rm -f "$list"
    done
fi

now=$(date -Iseconds)
prev_ok=$(python3 -c "import json;print(json.load(open('$STATUS')).get('last_ok',''))" 2>/dev/null)
[ $rc -eq 0 ] && [ -z "$DRY" ] && prev_ok=$now
printf '{"last_run": "%s", "last_ok": "%s", "rc": %d, "moved": %d}\n' "$now" "$prev_ok" "$rc" "$moved" > "$STATUS"
log "=== sync end rc=$rc moved=$moved"
[ -z "$DRY" ] && tail -n 20 "$LOG" | $SSH "$BIGPC" "cat >> '$DEST/logs/sync.log'" 2>/dev/null
tail -n 300 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
exit $rc
