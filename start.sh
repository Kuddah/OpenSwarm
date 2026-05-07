#!/usr/bin/env bash
# start.sh — one-shot setup + systemd service install for Ubuntu droplets
# Usage (as root):  bash start.sh
#
# What it does:
#   1. Installs system packages (python3, pip, venv, chromium deps)
#   2. Creates a .venv and installs Python requirements
#   3. Installs Playwright's Chromium browser
#   4. Registers and starts openswarm.service (auto-restart on crash/reboot)
set -e

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$REPO_DIR/.venv"
SERVICE_NAME="openswarm"
SERVICE_SRC="$REPO_DIR/openswarm.service"
SERVICE_DEST="/etc/systemd/system/$SERVICE_NAME.service"

# ── 1. System packages ───────────────────────────────────────────────────────
echo "==> Installing system packages..."
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip \
    libglib2.0-0 libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libdbus-1-3 libxkbcommon0 libx11-6 libxcomposite1 \
    libxdamage1 libxext6 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 \
    libcairo2 libasound2

echo "==> Python version: $(python3 --version)"

# ── 2. Virtual environment ───────────────────────────────────────────────────
if [ ! -d "$VENV_DIR" ]; then
    echo "==> Creating virtual environment..."
    python3 -m venv "$VENV_DIR"
fi

source "$VENV_DIR/bin/activate"

echo "==> Installing Python requirements..."
pip install --upgrade pip --quiet
pip install -r "$REPO_DIR/requirements.txt"

# ── 3. Playwright browser ────────────────────────────────────────────────────
echo "==> Installing Playwright Chromium..."
playwright install chromium 2>/dev/null || true

# ── 4. Patch service file with actual repo path & install ────────────────────
echo "==> Installing systemd service..."

# Write a copy with the correct WorkingDirectory / ExecStart for this machine
sed \
    -e "s|WorkingDirectory=.*|WorkingDirectory=$REPO_DIR|" \
    -e "s|EnvironmentFile=.*|EnvironmentFile=$REPO_DIR/.env|" \
    -e "s|ExecStart=.*|ExecStart=$VENV_DIR/bin/python $REPO_DIR/discord_bot.py|" \
    "$SERVICE_SRC" > "$SERVICE_DEST"

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"

echo ""
echo "==> Done! Service status:"
systemctl status "$SERVICE_NAME" --no-pager

echo ""
echo "Useful commands:"
echo "  View logs:    journalctl -u $SERVICE_NAME -f"
echo "  Stop bot:     systemctl stop $SERVICE_NAME"
echo "  Restart bot:  systemctl restart $SERVICE_NAME"
