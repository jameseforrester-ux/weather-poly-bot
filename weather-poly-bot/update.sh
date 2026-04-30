#!/usr/bin/env bash
# ─────────────────────────────────────────────
#  WeatherPolyBot — Hot-update script
#  Run:  sudo bash /opt/weather-poly-bot/update.sh
# ─────────────────────────────────────────────
set -euo pipefail

INSTALL_DIR="/opt/weather-poly-bot"
SERVICE="weatherpolybot.service"

echo "🔄 Stopping bot…"
systemctl stop "$SERVICE" || true

echo "📥 Pulling latest code…"
git -C "$INSTALL_DIR" pull --ff-only

echo "📦 Installing/updating Python deps…"
"$INSTALL_DIR/venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt" -q

echo "🔐 Fixing permissions…"
chown -R weatherbot:weatherbot "$INSTALL_DIR"

echo "🚀 Starting bot…"
systemctl start "$SERVICE"

echo "✅ Update complete!"
systemctl status "$SERVICE" --no-pager -l
