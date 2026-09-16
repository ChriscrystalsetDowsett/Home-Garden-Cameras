#!/usr/bin/env bash
# Nightly rsync of photos/videos to bigpc's media archive.
#
# Hardened vs v1:
#   * set -euo pipefail — no silent failure
#   * `--remove-source-files` runs ONLY after rsync returns 0 (data-loss guard)
#   * PROJECT_DIR resolved from the script's own path — no hardcoded prefixes
#   * mkdir -p on the log dir before writing
#   * Remote log write is best-effort; local log is always attempted

set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")"/.. && pwd -P)"
LOCAL_LOG_DIR="/home/chris/dev/logs"
LOCAL_LOG="${LOCAL_LOG_DIR}/sync-to-bigpc.log"
DEST="chris@100.75.224.125:/mnt/storage/media/pi4"
REMOTE_LOG="/mnt/storage/media/pi4/logs/sync.log"

mkdir -p "$LOCAL_LOG_DIR"

_ts() { date '+%Y-%m-%d %H:%M:%S'; }

log() {
    local line="$1"
    printf '%s\n' "$line" >> "$LOCAL_LOG"
    # Best-effort remote log; ignore SSH failures — local log is authoritative.
    ssh -o ConnectTimeout=5 chris@100.75.224.125 "cat >> $REMOTE_LOG" <<< "$line" 2>/dev/null || true
}

log ""
log "=== sync-to-bigpc started: $(_ts) ==="
log "project=${PROJECT_DIR}"

rsync_kind() {
    # Two-pass sync: transfer first, THEN delete locals only if rsync succeeded.
    # Prevents data loss when rsync partially fails and --remove-source-files
    # would otherwise trash unsynced photos.
    local kind="$1"
    local src="${PROJECT_DIR}/data/${kind}"
    local dst="${DEST}/${kind}/"

    if [ ! -d "$src" ]; then
        log "--- ${kind}/ not found locally, skipping ---"
        return 0
    fi

    log "--- rsyncing ${kind} (copy pass) ---"
    if ! rsync -avz --ignore-missing-args --exclude='*.thumb.jpg' \
            "${src}/" "$dst" >>"$LOCAL_LOG" 2>&1; then
        log "ERROR: rsync ${kind} FAILED — leaving source files in place"
        return 1
    fi

    log "--- pruning synced ${kind} sources ---"
    if ! rsync -av --ignore-missing-args --exclude='*.thumb.jpg' \
            --remove-source-files "${src}/" "$dst" >>"$LOCAL_LOG" 2>&1; then
        log "WARN: prune pass for ${kind} failed — sources retained"
        return 0   # non-fatal: files are safely at destination already
    fi
    return 0
}

rc=0
rsync_kind photos || rc=1
rsync_kind videos || rc=1

log "=== sync-to-bigpc finished: $(_ts) rc=${rc} ==="
exit "$rc"
