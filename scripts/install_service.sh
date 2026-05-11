#!/usr/bin/env bash
# install_service.sh — Install garden-monitor as a systemd service.
#
# Substitutes {{USER}}, {{WORKING_DIR}}, and {{XDG_RUNTIME_DIR}} from the
# template and writes the unit file to /etc/systemd/system/.
#
# XDG_RUNTIME_DIR is required: without it PipeWire/PulseAudio is unreachable
# and audio streaming silently produces 0 bytes.
#
# Usage (run as the app user, not root):
#   bash scripts/install_service.sh
#   bash scripts/install_service.sh --force   # overwrite existing unit file
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
TEMPLATE="$SCRIPT_DIR/garden-monitor.service.template"
UNIT_FILE="/etc/systemd/system/garden-monitor.service"

if [ ! -f "$TEMPLATE" ]; then
    echo "ERROR: template not found at $TEMPLATE" >&2
    exit 1
fi

if [ -f "$UNIT_FILE" ] && [[ "${1:-}" != "--force" ]]; then
    echo "==> $UNIT_FILE already exists. Pass --force to overwrite."
    exit 0
fi

SUBST_USER="$(whoami)"
SUBST_WORKING_DIR="$PROJECT_DIR"
SUBST_XDG="$(echo /run/user/$(id -u))"

echo "==> Installing garden-monitor.service"
echo "    User          : $SUBST_USER"
echo "    WorkingDir    : $SUBST_WORKING_DIR"
echo "    XDG_RUNTIME_DIR: $SUBST_XDG"

sed \
    -e "s|{{USER}}|$SUBST_USER|g" \
    -e "s|{{WORKING_DIR}}|$SUBST_WORKING_DIR|g" \
    -e "s|{{XDG_RUNTIME_DIR}}|$SUBST_XDG|g" \
    "$TEMPLATE" | sudo tee "$UNIT_FILE" > /dev/null

sudo systemctl daemon-reload
sudo systemctl enable garden-monitor

echo "==> Done. Start with: sudo systemctl start garden-monitor"
