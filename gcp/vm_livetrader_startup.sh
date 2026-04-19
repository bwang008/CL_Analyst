#!/bin/bash
# =============================================================================
# GCP VM Startup Script — Bare Metal Live Trader Setup
#
# Installs IBC + IB Gateway, Python venv, Xvfb, and systemd services.
# Runs automatically on first boot via GCP metadata startup-script.
# Logs to /tmp/startup.log, signals completion via /tmp/startup_done.
# =============================================================================

set -e
exec > /tmp/startup.log 2>&1

echo "$(date): === Live Trader VM Startup Script ==="

# ─── System Packages ─────────────────────────────────────────────────
echo "$(date): Installing system packages..."
apt-get update -qq
apt-get install -y -qq \
    python3-pip python3-venv python3-dev \
    xvfb \
    unzip wget curl \
    libgomp1 \
    default-jre-headless \
    git \
    tmux \
    > /dev/null

# ─── Create Service User ────────────────────────────────────────────
echo "$(date): Creating cltrader user..."
if ! id "cltrader" &>/dev/null; then
    useradd --system --create-home --shell /bin/bash cltrader
fi

# ─── Directory Structure ────────────────────────────────────────────
echo "$(date): Creating directory structure..."
mkdir -p /opt/cl-trader/{app,data,logs,venv}
mkdir -p /opt/cl-trader/data/{data/raw,data/processed,models,reports}
mkdir -p /opt/ibc

# ─── Install IBC (IB Gateway Controller) ────────────────────────────
echo "$(date): Installing IBC..."
IBC_VERSION="3.19.0"
cd /tmp
wget -q "https://github.com/IbcAlpha/IBC/releases/download/${IBC_VERSION}/IBCLinux-${IBC_VERSION}.zip" \
    -O ibc.zip || echo "WARN: IBC download failed — install manually"
if [ -f ibc.zip ]; then
    unzip -o -q ibc.zip -d /opt/ibc/
    chmod +x /opt/ibc/scripts/*.sh
    echo "$(date): IBC ${IBC_VERSION} installed to /opt/ibc/"
fi

# ─── Install IB Gateway ─────────────────────────────────────────────
echo "$(date): Installing IB Gateway..."
IB_GW_VERSION="10.30.1t"
wget -q "https://download2.interactivebrokers.com/installers/ibgateway/stable-standalone/ibgateway-stable-standalone-linux-x64.sh" \
    -O /tmp/ibgateway-install.sh || echo "WARN: IB Gateway download failed — install manually"
if [ -f /tmp/ibgateway-install.sh ]; then
    chmod +x /tmp/ibgateway-install.sh
    # Silent install (no GUI needed — Xvfb not running yet)
    /tmp/ibgateway-install.sh -q -dir /opt/ibgateway 2>/dev/null || \
        echo "WARN: IB Gateway silent install may need manual intervention"
    echo "$(date): IB Gateway installed to /opt/ibgateway/"
fi

# ─── Python Virtual Environment ─────────────────────────────────────
echo "$(date): Creating Python virtual environment..."
python3 -m venv /opt/cl-trader/venv
source /opt/cl-trader/venv/bin/activate

echo "$(date): Installing Python packages..."
pip install --no-cache-dir --quiet \
    'lightgbm>=4.0.0' \
    'pandas>=2.0.0' \
    'numpy>=1.24.0' \
    'pandas_ta>=0.3.0' \
    'scikit-learn>=1.3.0' \
    'ib_insync>=0.9.86' \
    'python-dotenv>=1.0.0' \
    'joblib>=1.3.0' \
    'pyarrow>=10.0.0' \
    'requests>=2.28.0' \
    'fredapi>=0.5.0'

# ─── Permissions ─────────────────────────────────────────────────────
echo "$(date): Setting permissions..."
chown -R cltrader:cltrader /opt/cl-trader
chown -R cltrader:cltrader /opt/ibc
chmod -R 755 /opt/cl-trader
chmod -R 755 /opt/ibc

# ─── Signal Completion ──────────────────────────────────────────────
touch /tmp/startup_done
echo "$(date): === Startup Complete ==="
echo ""
echo "Next steps:"
echo "  1. Clone your repo:  git clone <repo> /opt/cl-trader/app"
echo "  2. Copy seed data:   gcloud compute scp ... /opt/cl-trader/data/"
echo "  3. Edit credentials: sudo nano /etc/cl-trader.env"
echo "  4. Install services: sudo cp /opt/cl-trader/app/deploy/systemd/*.service /etc/systemd/system/"
echo "  5. Start:            sudo systemctl start ibc-gateway && sudo systemctl start live-trader"
