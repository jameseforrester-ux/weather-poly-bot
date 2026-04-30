#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
#  WeatherPolyBot — One-shot deploy script for Ubuntu/Debian VPS
#  Run as root:  sudo bash deploy.sh
# ═══════════════════════════════════════════════════════════════
set -euo pipefail

REPO_URL="https://github.com/YOUR_USERNAME/weather-poly-bot.git"
INSTALL_DIR="/opt/weather-poly-bot"
SERVICE_USER="weatherbot"
SERVICE_FILE="weatherpolybot.service"
PYTHON_MIN="3.11"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

info()    { echo -e "${CYAN}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# ── Root check ────────────────────────────────────────────
[[ $EUID -ne 0 ]] && error "Please run as root: sudo bash deploy.sh"

echo -e "${BOLD}"
echo "╔══════════════════════════════════════════╗"
echo "║   WeatherPolyBot  —  Deploy Script       ║"
echo "╚══════════════════════════════════════════╝"
echo -e "${NC}"

# ── 1. System packages ───────────────────────────────────
info "Updating apt and installing dependencies…"
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv git curl > /dev/null
success "System packages ready"

# ── 2. Python version check ──────────────────────────────
PYVER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
info "Python version: $PYVER"
python3 -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)" \
    || error "Python ≥ 3.11 required. Install via deadsnakes PPA: sudo add-apt-repository ppa:deadsnakes/ppa"

# ── 3. Service user ──────────────────────────────────────
if ! id "$SERVICE_USER" &>/dev/null; then
    info "Creating system user '$SERVICE_USER'…"
    useradd --system --no-create-home --shell /sbin/nologin "$SERVICE_USER"
    success "User '$SERVICE_USER' created"
else
    info "User '$SERVICE_USER' already exists"
fi

# ── 4. Clone or update repo ──────────────────────────────
if [[ -d "$INSTALL_DIR/.git" ]]; then
    info "Updating existing repo…"
    git -C "$INSTALL_DIR" pull --ff-only
else
    info "Cloning repo to $INSTALL_DIR…"
    git clone "$REPO_URL" "$INSTALL_DIR"
fi
success "Code at $INSTALL_DIR"

# ── 5. Python venv + deps ────────────────────────────────
info "Creating virtual environment…"
python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install --upgrade pip -q
"$INSTALL_DIR/venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt" -q
success "Python deps installed"

# ── 6. .env file ─────────────────────────────────────────
if [[ ! -f "$INSTALL_DIR/.env" ]]; then
    cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
    warn ".env file created — you MUST edit it before starting:"
    warn "  nano $INSTALL_DIR/.env"
    warn "  Set TELEGRAM_BOT_TOKEN=<your token>"
else
    info ".env already exists — skipping"
fi

# ── 7. Permissions ───────────────────────────────────────
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
chmod 640 "$INSTALL_DIR/.env"
success "Permissions set"

# ── 8. Systemd service ───────────────────────────────────
info "Installing systemd service…"
cp "$INSTALL_DIR/$SERVICE_FILE" "/etc/systemd/system/$SERVICE_FILE"
systemctl daemon-reload
systemctl enable "$SERVICE_FILE"
success "Service enabled (will auto-start on reboot)"

# ── 9. First-run prompt ──────────────────────────────────
echo ""
echo -e "${BOLD}${GREEN}═══════════════════════════════════════════${NC}"
echo -e "${BOLD}  Deploy complete! Follow these final steps:${NC}"
echo -e "${BOLD}${GREEN}═══════════════════════════════════════════${NC}"
echo ""
echo -e "  ${YELLOW}1.${NC} Edit your bot token:"
echo -e "     ${CYAN}nano $INSTALL_DIR/.env${NC}"
echo ""
echo -e "  ${YELLOW}2.${NC} Start the bot:"
echo -e "     ${CYAN}sudo systemctl start $SERVICE_FILE${NC}"
echo ""
echo -e "  ${YELLOW}3.${NC} Check it's running:"
echo -e "     ${CYAN}sudo systemctl status $SERVICE_FILE${NC}"
echo ""
echo -e "  ${YELLOW}4.${NC} Watch live logs:"
echo -e "     ${CYAN}sudo journalctl -u $SERVICE_FILE -f${NC}"
echo ""
echo -e "  ${YELLOW}5.${NC} Update to latest code anytime:"
echo -e "     ${CYAN}sudo bash $INSTALL_DIR/update.sh${NC}"
echo ""
