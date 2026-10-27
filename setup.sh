#!/usr/bin/env bash
# One-time setup for Zorin OS / Ubuntu-based Linux.
# Idempotent: safe to re-run.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

echo "==> Installing system dependencies (sudo required)…"
sudo apt update
sudo apt install -y ffmpeg python3 python3-venv python3-pip

echo "==> Creating virtual environment (.venv)…"
if [ ! -d .venv ]; then
    python3 -m venv .venv
fi

echo "==> Installing Python dependencies…"
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo "==> Installing launcher, app command, and icon…"
mkdir -p "$HOME/.local/bin"
mkdir -p "$HOME/.local/share/applications"
mkdir -p "$HOME/.local/share/icons/hicolor/scalable/apps"

ln -sf "$PROJECT_DIR/run.sh" "$HOME/.local/bin/duplicate-video-finder"
cp "$PROJECT_DIR/resources/icons/duplicate-video-finder.svg" \
   "$HOME/.local/share/icons/hicolor/scalable/apps/duplicate-video-finder.svg"
cp "$PROJECT_DIR/duplicate-video-finder.desktop" \
   "$HOME/.local/share/applications/duplicate-video-finder.desktop"
sed -i "s|^Exec=.*|Exec=$PROJECT_DIR/run.sh|" "$HOME/.local/share/applications/duplicate-video-finder.desktop"
chmod +x "$HOME/.local/share/applications/duplicate-video-finder.desktop"

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$HOME/.local/share/applications" || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -q "$HOME/.local/share/icons/hicolor" || true
fi

echo
echo "Setup complete."
echo "Run the app with:  ./run.sh"
