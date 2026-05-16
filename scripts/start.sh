#!/usr/bin/env bash
# start.sh — Start Home Garden Cameras inside a persistent tmux session.
#
# Usage:
#   bash scripts/start.sh          # start in background tmux session
#   bash scripts/start.sh --fg     # run in foreground (Ctrl-C to stop)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
SESSION="home-garden-cameras"
PORT="$(python3 -c "import yaml; c=yaml.safe_load(open('$PROJECT_DIR/config/settings.yaml')); print(c['server']['port'])" 2>/dev/null || echo 8080)"

cd "$PROJECT_DIR"

if [[ "${1:-}" == "--fg" ]]; then
    echo "==> Starting Home Garden Cameras on port $PORT (foreground) …"
    exec python3 run.py
fi

# Background mode — use tmux so the session survives SSH disconnection
if tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "==> Home Garden Cameras is already running (tmux session: $SESSION)"
    echo "    Attach with: tmux attach -t $SESSION"
    exit 0
fi

tmux new-session -d -s "$SESSION" "cd '$PROJECT_DIR' && python3 run.py"
sleep 1

IP="$(hostname -I | awk '{print $1}')"
echo "==> Home Garden Cameras started on port $PORT"
echo "    Web UI:  http://$IP:$PORT"
echo "    Logs:    tmux attach -t $SESSION"
echo "    Stop:    bash scripts/stop.sh"
