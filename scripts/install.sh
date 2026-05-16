#!/usr/bin/env bash
# install.sh — Bootstrap Home Garden Cameras on a fresh Raspberry Pi OS Lite (Bookworm)
# Run as the pi user (NOT root).  It will sudo when needed.
#
# Usage:
#   cd ~/home-garden-cameras
#   bash scripts/install.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

echo "==> Home Garden Cameras install started"
echo "    Project root: $PROJECT_DIR"

# ── 1. System packages ─────────────────────────────────────────────────────────
echo ""
echo "==> Installing system dependencies …"
sudo apt-get update -qq
sudo apt-get install -y --no-install-recommends \
    python3-pip \
    python3-picamera2 \
    ffmpeg \
    libatlas-base-dev \
    libopenjp2-7 \
    libtiff6 \
    tmux

# ── 2. Python packages ─────────────────────────────────────────────────────────
echo ""
echo "==> Installing Python packages …"
pip3 install --upgrade pip --quiet
pip3 install -r "$PROJECT_DIR/requirements.txt" --quiet

# ── 3. Data directories ────────────────────────────────────────────────────────
echo ""
echo "==> Creating data directories …"
mkdir -p "$PROJECT_DIR/data/photos"
mkdir -p "$PROJECT_DIR/data/videos"

# ── 4. Device config ───────────────────────────────────────────────────────────
SETTINGS="$PROJECT_DIR/config/settings.yaml"
EXAMPLE="$PROJECT_DIR/config/settings.yaml.example"
if [ ! -f "$SETTINGS" ]; then
    echo ""
    echo "==> No config/settings.yaml found — copying from example …"
    cp "$EXAMPLE" "$SETTINGS"
    echo "    Edit $SETTINGS to set your camera name, port, etc."
fi

# ── 5. Make scripts executable ────────────────────────────────────────────────
chmod +x "$SCRIPT_DIR"/*.sh

# ── 6. Optional: install as a systemd service ─────────────────────────────────
UNIT_FILE="/etc/systemd/system/home-garden-cameras.service"
if [ ! -f "$UNIT_FILE" ]; then
    echo ""
    read -rp "==> Install as a systemd service (starts on boot)? [y/N] " REPLY
    if [[ "${REPLY,,}" == "y" ]]; then
        bash "$SCRIPT_DIR/install_service.sh"
        echo "    Systemd service installed. Start it with: sudo systemctl start home-garden-cameras"
    fi
fi

echo ""
echo "==> Installation complete."
echo "    Start the app with:  bash scripts/start.sh"
echo "    Or directly with:    cd $PROJECT_DIR && python3 run.py"
