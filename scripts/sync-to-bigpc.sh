#!/bin/bash

HOSTNAME=pi4
DEST=chris@100.75.224.125:/mnt/storage/media/pi4
REMOTE_LOG=/mnt/storage/media/pi4/logs/sync.log
LOCAL_LOG=/home/chris/dev/logs/sync-to-bigpc.log
TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')

log() {
    echo "$1" | tee -a "$LOCAL_LOG" | ssh chris@100.75.224.125 "cat >> $REMOTE_LOG"
}

log ""
log "=== sync-to-bigpc started: $TIMESTAMP ==="

if [ -d /home/chris/dev/apps/home-garden-cameras/data/photos ]; then
    log "--- rsyncing photos ---"
    rsync -avz --ignore-missing-args --exclude='*.thumb.jpg' --remove-source-files \
        /home/chris/dev/apps/home-garden-cameras/data/photos/ "$DEST/photos/" \
        2>&1 | tee -a "$LOCAL_LOG" | ssh chris@100.75.224.125 "cat >> $REMOTE_LOG"
else
    log "photos/ not found locally, skipping"
fi

if [ -d /home/chris/dev/apps/home-garden-cameras/data/videos ]; then
    log "--- rsyncing videos ---"
    rsync -avz --ignore-missing-args --exclude='*.thumb.jpg' --remove-source-files \
        /home/chris/dev/apps/home-garden-cameras/data/videos/ "$DEST/videos/" \
        2>&1 | tee -a "$LOCAL_LOG" | ssh chris@100.75.224.125 "cat >> $REMOTE_LOG"
else
    log "videos/ not found locally, skipping"
fi

log "=== sync-to-bigpc finished: $(date '+%Y-%m-%d %H:%M:%S') ==="
