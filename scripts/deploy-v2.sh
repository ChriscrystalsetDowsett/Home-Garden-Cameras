#!/usr/bin/env bash
# deploy-v2.sh — stage, cut over, and (if needed) roll back the v2 camera app
# on pi4. Run from the v2 worktree on bigpc.
#
# Usage:
#   scripts/deploy-v2.sh                # preflight (tests) + stage to pi4 only
#   scripts/deploy-v2.sh --cutover      # preflight + stage + atomic swap + verify
#   scripts/deploy-v2.sh --rollback     # restore the frozen v1 tree
#
# The systemd unit and crontab are NOT touched: the unit points at
# /home/chris/dev/apps/home-garden-cameras, whose contents are swapped
# underneath it, and every cron path is identical in the v2 tree.
#
# Verification failure after the swap triggers an automatic rollback.

set -euo pipefail

PI="${PI:-pi4}"    # ~/.ssh/config alias — carries the pi4_sync identity file
PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")"/.. && pwd -P)"
APPS=/home/chris/dev/apps
LIVE="$APPS/home-garden-cameras"
STAGE="$APPS/home-garden-cameras-v2"
FROZEN="$APPS/home-garden-cameras.v1-frozen"

say()  { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
fail() { printf '\n\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

MODE="stage"
case "${1:-}" in
    --cutover)  MODE="cutover" ;;
    --rollback) MODE="rollback" ;;
    "")         ;;
    *) fail "unknown flag: $1 (use --cutover or --rollback)" ;;
esac

# ── Rollback: restore v1 and exit ─────────────────────────────────────────────
if [ "$MODE" = "rollback" ]; then
    say "Rolling back to frozen v1 on $PI"
    ssh "$PI" bash -s -- "$LIVE" "$FROZEN" <<'REMOTE'
set -euo pipefail
LIVE="$1"; FROZEN="$2"
[ -d "$FROZEN" ] || { echo "no frozen v1 tree at $FROZEN — nothing to roll back to"; exit 1; }
sudo systemctl stop garden-monitor
TS=$(date +%Y%m%d-%H%M%S)
if [ -d "$LIVE" ]; then
    mv "$LIVE" "$LIVE.v2-failed-$TS"
fi
mv "$FROZEN" "$LIVE"
# Bring back any media captured while v2 was live (additive, no deletes).
if [ -d "$LIVE.v2-failed-$TS/data" ]; then
    rsync -a --ignore-existing "$LIVE.v2-failed-$TS/data/" "$LIVE/data/" || true
fi
sudo systemctl start garden-monitor
sleep 3
code=$(curl -s -o /dev/null -w '%{http_code}' http://localhost:8080/ || true)
echo "root route after rollback: HTTP $code"
[ "$code" = 200 ] || exit 1
echo "v1 restored. Failed v2 tree kept at $LIVE.v2-failed-$TS"
REMOTE
    say "Rollback complete."
    exit 0
fi

# ── Preflight: full test suite + syntax gates on bigpc ────────────────────────
say "Preflight: running test suite"
cd "$PROJECT_DIR"
PY=python3; [ -x .venv/bin/python ] && PY=.venv/bin/python
"$PY" -m pytest tests/ -q || fail "tests failed — refusing to deploy"
"$PY" -m compileall -q app run.py || fail "compileall failed"
command -v node >/dev/null && {
    node --check static/index.js     || fail "index.js syntax error"
    node --check static/dashboard.js || fail "dashboard.js syntax error"
}

# ── Stage: rsync the v2 tree next to the live app ─────────────────────────────
say "Staging v2 tree to $PI:$STAGE"
ssh "$PI" "mkdir -p '$STAGE'"
rsync -az --delete \
    --exclude '.git' --exclude '.venv' --exclude '__pycache__' \
    --exclude '.pytest_cache' --exclude 'data/' \
    "$PROJECT_DIR/" "$PI:$STAGE/"
ssh "$PI" "mkdir -p '$STAGE/data/photos' '$STAGE/data/videos' '$STAGE/data/logs'"

# ── Settings sanity: staged config must carry over the live camera setup ──────
say "Checking staged settings.yaml against the live one"
ssh "$PI" bash -s -- "$LIVE" "$STAGE" <<'REMOTE' || fail "settings check failed — reconcile config/settings.yaml first"
set -euo pipefail
LIVE="$1"; STAGE="$2"
python3 - "$LIVE/config/settings.yaml" "$STAGE/config/settings.yaml" <<'PY'
import sys, yaml
live = yaml.safe_load(open(sys.argv[1]))
new  = yaml.safe_load(open(sys.argv[2]))

def get(d, path):
    for k in path.split("."):
        d = (d or {}).get(k)
    return d

# Keys that must carry over from the running v1 config unchanged.
carry = ["server.host", "server.port", "server.pi_name",
         "camera.backend", "camera.default_resolution",
         "camera.hflip", "camera.vflip",
         "schedule.enabled", "schedule.camera_off", "schedule.camera_on",
         "dashboard.password"]

def norm(v):
    # Absent key (None) and empty string are the same "unset" state.
    return None if v in (None, "") else v

bad = [(k, get(live, k), get(new, k)) for k in carry
       if norm(get(live, k)) != norm(get(new, k))]
for k, lv, nv in bad:
    print(f"  MISMATCH {k}: live={lv!r} staged={nv!r}")

# Keys v2 requires to be present and non-placeholder.
missing = [k for k in ("server.api_token", "dashboard.secret_key")
           if not get(new, k) or get(new, k) in ("changeme", "dev-secret-key")]
for k in missing:
    print(f"  MISSING/PLACEHOLDER {k} in staged settings")

sys.exit(1 if (bad or missing) else 0)
PY
REMOTE

if [ "$MODE" = "stage" ]; then
    say "Staged only (no cutover). Re-run with --cutover to swap."
    exit 0
fi

# ── Cutover: atomic swap + verify, auto-rollback on failure ───────────────────
say "Cutover: swapping v2 into place on $PI"
ssh "$PI" bash -s -- "$LIVE" "$STAGE" "$FROZEN" <<'REMOTE'
set -euo pipefail
LIVE="$1"; STAGE="$2"; FROZEN="$3"

[ -d "$LIVE" ]     || { echo "live tree missing at $LIVE"; exit 1; }
[ -d "$STAGE" ]    || { echo "staged v2 tree missing at $STAGE"; exit 1; }
[ ! -e "$FROZEN" ] || { echo "$FROZEN already exists — clean up the previous attempt first"; exit 1; }

rollback() {
    echo "!! verification failed — rolling back to v1"
    sudo systemctl stop garden-monitor || true
    TS=$(date +%Y%m%d-%H%M%S)
    [ -d "$LIVE" ] && mv "$LIVE" "$LIVE.v2-failed-$TS"
    mv "$FROZEN" "$LIVE"
    sudo systemctl start garden-monitor
    sleep 3
    code=$(curl -s -o /dev/null -w '%{http_code}' http://localhost:8080/ || true)
    echo "root route after rollback: HTTP $code"
    exit 1
}

echo "-- stopping garden-monitor"
sudo systemctl stop garden-monitor

echo "-- swapping trees"
mv "$LIVE" "$FROZEN"
mv "$STAGE" "$LIVE"

echo "-- carrying over captured media (hardlinks)"
for kind in photos videos; do
    if [ -d "$FROZEN/data/$kind" ]; then
        cp -al "$FROZEN/data/$kind/." "$LIVE/data/$kind/" 2>/dev/null \
            || cp -a "$FROZEN/data/$kind/." "$LIVE/data/$kind/"
    fi
done

echo "-- starting garden-monitor"
sudo systemctl start garden-monitor

echo "-- waiting for the app to come up"
up=""
for i in $(seq 1 15); do
    code=$(curl -s -o /dev/null -w '%{http_code}' http://localhost:8080/ || true)
    [ "$code" = 200 ] && { up=1; break; }
    sleep 2
done
[ -n "$up" ] || rollback

echo "-- health check"
curl -s http://localhost:8080/api/health || rollback
echo

echo "-- synthetic GH capture"
resp=$(curl -s -X POST http://localhost:8080/api/snapshot \
        -H 'Content-Type: application/json' -d '{"label":"GH"}' || true)
echo "   $resp"
fname=$(printf '%s' "$resp" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("filename","") if d.get("ok") else "")' 2>/dev/null || true)
[ -n "$fname" ] || rollback
sleep 2
[ -f "$LIVE/data/photos/$fname" ] || rollback
thumb="${fname%.jpg}.thumb.jpg"
[ -f "$LIVE/data/photos/$thumb" ] || rollback

echo "-- LAN auth check (POST without token must be 401)"
LAN_IP=$(hostname -I | awk '{print $1}')
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "http://$LAN_IP:8080/api/snapshot" \
        -H 'Content-Type: application/json' -d '{"label":"T"}' || true)
[ "$code" = 401 ] || { echo "expected 401 from LAN without token, got $code"; rollback; }

echo
echo "CUTOVER OK — v2 live. Frozen v1 kept at $FROZEN"
echo "Watch: journalctl -u garden-monitor -f  |  next cron tick lands a GH_*.jpg"
REMOTE

say "Cutover complete. Keep $FROZEN for the 48 h watch window, then prune."
say "Manual rollback at any time: scripts/deploy-v2.sh --rollback"
