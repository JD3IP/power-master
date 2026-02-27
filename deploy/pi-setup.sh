#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────
# Power Master — Raspberry Pi 3B Setup Script
# ─────────────────────────────────────────────────────────
# Run on a fresh Raspberry Pi OS (64-bit Lite recommended):
#   curl -sL <repo-url>/deploy/pi-setup.sh | bash
#   — or —
#   bash deploy/pi-setup.sh
#
# What this script does:
#   1. Installs Docker + docker compose plugin
#   2. Configures 2GB swap (needed for pip compile during build)
#   3. Creates data directory for persistent storage
#   4. Copies default config for editing
# ─────────────────────────────────────────────────────────
set -euo pipefail

DATA_DIR="/opt/power-master/data"
SWAP_SIZE_MB=2048

echo "========================================"
echo " Power Master — Pi Setup"
echo "========================================"

# ── 1. Install Docker ──
if command -v docker &>/dev/null; then
    echo "[OK] Docker already installed: $(docker --version)"
else
    echo "[1/4] Installing Docker..."
    curl -fsSL https://get.docker.com | sh
    sudo usermod -aG docker "$USER"
    echo "[OK] Docker installed. You may need to log out and back in for group changes."
fi

# Check docker compose plugin
if docker compose version &>/dev/null; then
    echo "[OK] Docker Compose plugin available"
else
    echo "[!] Docker Compose plugin not found. Installing..."
    sudo apt-get update
    sudo apt-get install -y docker-compose-plugin
fi

# ── 2. Configure swap ──
CURRENT_SWAP=$(free -m | awk '/Swap:/{print $2}')
if [ "$CURRENT_SWAP" -lt "$SWAP_SIZE_MB" ]; then
    echo "[2/4] Configuring ${SWAP_SIZE_MB}MB swap (currently ${CURRENT_SWAP}MB)..."

    # Raspberry Pi OS uses dphys-swapfile
    if [ -f /etc/dphys-swapfile ]; then
        sudo sed -i "s/^CONF_SWAPSIZE=.*/CONF_SWAPSIZE=${SWAP_SIZE_MB}/" /etc/dphys-swapfile
        sudo systemctl restart dphys-swapfile
        echo "[OK] Swap configured via dphys-swapfile"
    else
        # Fallback: create swap file manually
        sudo fallocate -l ${SWAP_SIZE_MB}M /swapfile
        sudo chmod 600 /swapfile
        sudo mkswap /swapfile
        sudo swapon /swapfile
        echo "/swapfile none swap sw 0 0" | sudo tee -a /etc/fstab
        echo "[OK] Swap file created"
    fi
else
    echo "[OK] Swap already ${CURRENT_SWAP}MB (>= ${SWAP_SIZE_MB}MB)"
fi

# ── 3. Create data directory ──
echo "[3/4] Creating data directory at ${DATA_DIR}..."
sudo mkdir -p "$DATA_DIR"
sudo chown "$(id -u):$(id -g)" "$DATA_DIR"

# ── 4. Copy default config ──
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -f "${SCRIPT_DIR}/config.defaults.yaml" ] && [ ! -f "${DATA_DIR}/config.yaml" ]; then
    cp "${SCRIPT_DIR}/config.defaults.yaml" "${DATA_DIR}/config.yaml"
    echo "[OK] Default config copied to ${DATA_DIR}/config.yaml"
    echo ""
    echo "  >>> IMPORTANT: Edit ${DATA_DIR}/config.yaml with your settings:"
    echo "      - hardware.foxess.host  (your inverter IP)"
    echo "      - providers.tariff      (Amber API key if applicable)"
    echo "      - providers.solar       (location coordinates)"
    echo "      - dashboard.auth        (enable + set password for remote access)"
    echo ""
elif [ -f "${DATA_DIR}/config.yaml" ]; then
    echo "[OK] Config already exists at ${DATA_DIR}/config.yaml"
else
    echo "[!] config.defaults.yaml not found at ${SCRIPT_DIR}/"
    echo "    Copy config.defaults.yaml to ${DATA_DIR}/config.yaml manually."
fi

echo "========================================"
echo " Setup complete!"
echo ""
echo " Next steps:"
echo "   1. Edit config:  nano ${DATA_DIR}/config.yaml"
echo "   2. Build image:  docker compose -f docker-compose.pi.yml build"
echo "      (or load pre-built: docker load -i power-master-pi.tar)"
echo "   3. Start:        docker compose -f docker-compose.pi.yml up -d"
echo "   4. Dashboard:    http://$(hostname -I | awk '{print $1}'):8080"
echo "   5. Logs:         docker logs -f power-master"
echo "========================================"
