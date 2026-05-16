#!/usr/bin/env bash
# stop.sh — Stop Home Garden Cameras gracefully.
set -euo pipefail

SESSION="home-garden-cameras"

if tmux has-session -t "$SESSION" 2>/dev/null; then
    tmux send-keys -t "$SESSION" C-c ""
    sleep 2
    tmux kill-session -t "$SESSION" 2>/dev/null || true
    echo "==> Home Garden Cameras stopped (tmux session '$SESSION' closed)."
else
    # Fallback: kill any python3 run.py process
    if pkill -f "python3 run.py" 2>/dev/null; then
        echo "==> Home Garden Cameras process stopped."
    else
        echo "==> Home Garden Cameras does not appear to be running."
    fi
fi
