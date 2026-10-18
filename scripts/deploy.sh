#!/usr/bin/env bash
# deploy.sh <pi> — put this tree on one camera Pi, check it, roll back on failure.
#
#   scripts/deploy.sh pi4            tests, back up, copy, restart, verify
#   scripts/deploy.sh pi4 --dry-run  show what would change, touch nothing
#   scripts/deploy.sh pi4 --rollback restore the newest backup
#
# Every Pi runs the same code; only config/settings.yaml differs (never copied).
# Missing secrets are added to that file on the Pi: server.api_token (the fleet
# token from ~/.config/home-cameras/fleet-token on bigPC, so the wall's proxy can
# act on every camera) and dashboard.secret_key (random, per Pi).
set -euo pipefail

PI="${1:?usage: deploy.sh pi3|pi4|pi5 [--dry-run|--rollback]}"
MODE="${2:-deploy}"
case "$PI" in
  pi3) SSH_HOST=pi3;     SERVICE=camera-stream;  PORT=5000 ;;
  pi4) SSH_HOST=pi4;     SERVICE=garden-monitor; PORT=8080 ;;
  pi5) SSH_HOST=pi5-lan; SERVICE=garden-monitor; PORT=8080 ;;
  *) echo "unknown pi: $PI" >&2; exit 2 ;;
esac
LIVE=/home/chris/dev/apps/home-garden-cameras
SRC="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")"/.. && pwd -P)"
TOKEN_FILE="$HOME/.config/home-cameras/fleet-token"
say()  { printf '\033[1;32m==> %s\033[0m\n' "$*"; }
fail() { printf '\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

health() {   # prints the health JSON, or nothing
  ssh "$SSH_HOST" "curl -sf --max-time 5 http://127.0.0.1:$PORT/api/health" 2>/dev/null || true
}

rollback() {
  say "Rolling back $PI to the newest backup"
  ssh "$SSH_HOST" bash -s -- "$LIVE" "$SERVICE" <<'REMOTE'
set -e
b=$(ls -t ~/backups/hgc-*.tgz | head -1)
echo "restoring $b"
find "$1" -mindepth 1 -maxdepth 1 ! -name data ! -name config -exec rm -rf {} +
tar xzf "$b" -C "$1"
sudo -n systemctl restart "$2"
REMOTE
}

if [ "$MODE" = "--rollback" ]; then rollback; exit 0; fi

say "Tests"
(cd "$SRC" && make -s test >/tmp/deploy-tests.log 2>&1) || { tail -20 /tmp/deploy-tests.log; fail "tests failed"; }
[ -z "$(cd "$SRC" && git status --porcelain -- app static templates scripts run.py)" ] \
  || fail "uncommitted changes in $SRC; commit first so VERSION means something"
VERSION="$(cd "$SRC" && git rev-parse --short HEAD) $(cd "$SRC" && git log -1 --format=%cs)"

RSYNC_ARGS=(-a --delete --exclude __pycache__ --exclude '*.pyc')
if [ "$MODE" = "--dry-run" ]; then
  say "Dry run: changes on $PI"
  for d in app static templates scripts; do
    rsync "${RSYNC_ARGS[@]}" --dry-run --itemize-changes "$SRC/$d/" "$SSH_HOST:$LIVE/$d/" | sed "s|^|$d: |"
  done
  exit 0
fi

say "Backing up $PI's live tree"
ssh "$SSH_HOST" bash -s -- "$LIVE" <<'REMOTE'
set -e
mkdir -p ~/backups
ts=$(date +%Y%m%d-%H%M%S)
tar czf ~/backups/hgc-$ts.tgz --exclude=./data --exclude=./config/settings.yaml -C "$1" .
# The Pis are deployed from bigPC now; park the old git checkout so nobody pulls over it.
if [ -d "$1/.git" ]; then mv "$1/.git" ~/backups/hgc-git-$ts; fi
ls -t ~/backups/hgc-*.tgz | tail -n +6 | xargs -r rm -f      # keep 5
echo "backup ~/backups/hgc-$ts.tgz"
REMOTE

say "Checking secrets in $PI's settings.yaml"
ssh "$SSH_HOST" python3 - "$LIVE/config/settings.yaml" < <(cat <<'PY'; echo "TOKEN = '$(cat "$TOKEN_FILE")'"; echo "main()"
import re, secrets, sys, os
path = sys.argv[1]
def main():
    s = open(path).read()
    changed = []
    if not re.search(r"^\s+api_token:\s*\S", s, re.M):
        s = re.sub(r"^server:\s*\n", "server:\n  api_token: \"%s\"   # fleet token (writes); same on every Pi\n" % TOKEN, s, count=1, flags=re.M)
        changed.append("server.api_token")
    if not re.search(r"^\s+secret_key:\s*\S", s, re.M):
        key = secrets.token_hex(32)
        if re.search(r"^dashboard:\s*$", s, re.M):
            s = re.sub(r"^dashboard:\s*\n", "dashboard:\n  secret_key: \"%s\"\n" % key, s, count=1, flags=re.M)
        else:
            s += "\ndashboard:\n  secret_key: \"%s\"   # signs session cookies\n" % key
        changed.append("dashboard.secret_key")
    if changed:
        tmp = path + ".tmp"
        open(tmp, "w").write(s); os.chmod(tmp, 0o600); os.replace(tmp, path)
    os.chmod(path, 0o600)
    print("added: " + ", ".join(changed) if changed else "secrets present")
PY
)

say "Copying code to $PI"
for d in app static templates scripts; do
  rsync "${RSYNC_ARGS[@]}" "$SRC/$d/" "$SSH_HOST:$LIVE/$d/"
done
rsync -a "$SRC/run.py" "$SRC/requirements.txt" "$SSH_HOST:$LIVE/"
echo "$VERSION" | ssh "$SSH_HOST" "cat > $LIVE/VERSION"

say "Restarting $SERVICE"
ssh "$SSH_HOST" "sudo -n systemctl restart $SERVICE"
ok=""
for i in $(seq 1 20); do
  sleep 3
  h=$(health)
  if echo "$h" | python3 -c "import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get('camera')=='live' and d.get('version','').startswith('${VERSION%% *}') else 1)" 2>/dev/null; then
    ok=1; break
  fi
done
if [ -z "$ok" ]; then
  echo "last health: ${h:-<no answer>}"
  ssh "$SSH_HOST" "journalctl -u $SERVICE -n 30 --no-pager" | tail -30
  rollback
  fail "$PI did not come back healthy on $VERSION; rolled back"
fi
say "$PI healthy on $VERSION"
echo "$h" | python3 -m json.tool
