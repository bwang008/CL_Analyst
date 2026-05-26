#!/bin/bash
# =============================================================================
# CL Analyst — Headless Ubuntu/WSL Production Setup Script
# =============================================================================
# Purpose: Automates dependency installation, folder provisioning, IBC/Gateway
#          setup, systemd templates interpolation, and cloud GUI VNC prep.
# Environment: Agnostic. Detects current active user, home directory, and paths.
# Execution: Run with standard user privileges (script will prompt for sudo where needed).
# =============================================================================

set -euo pipefail

# --- Color Logging Helpers ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

info() { echo -e "${BLUE}[INFO]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }
success() { echo -e "${GREEN}[SUCCESS]${NC} $1"; }

# --- Host and User Detection ---
ACTIVE_USER=$(whoami)
USER_HOME=$HOME
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

info "Starting CL Analyst installation wrapper..."
info "Active User: ${ACTIVE_USER}"
info "Home Directory: ${USER_HOME}"
info "Project Directory: ${PROJECT_DIR}"

# Ensure we do not run as root directly (we want to provision user folders under standard user)
if [ "${ACTIVE_USER}" = "root" ]; then
    error "Do not run this script directly as root. Run as a standard user with sudo privileges."
fi

# =============================================================================
# Step 1: Install OS-Level Dependencies
# =============================================================================
info "Updating apt cache and installing required system packages..."
sudo apt-get update -y

sudo apt-get install -y \
    default-jre \
    xvfb \
    unzip \
    wget \
    curl \
    socat \
    net-tools \
    git \
    tightvncserver \
    logrotate

success "OS dependencies installed successfully."

# =============================================================================
# Step 2: Directory Scaffolding & Permissions
# =============================================================================
info "Creating application directory scaffolding under /opt/cl-trader/ and /opt/ibc/..."

# Provision standard trader data and log directories
sudo mkdir -p /opt/cl-trader/logs
sudo mkdir -p /opt/cl-trader/data
sudo mkdir -p /opt/cl-trader/data/models
sudo mkdir -p /opt/cl-trader/data/data
sudo mkdir -p /opt/ibc

# Hand ownership to the active deploy user (prevents permission errors during execution)
sudo chown -R "${ACTIVE_USER}:${ACTIVE_USER}" /opt/cl-trader
sudo chown -R "${ACTIVE_USER}:${ACTIVE_USER}" /opt/ibc

success "Scaffolding created and chowned to ${ACTIVE_USER}."

# =============================================================================
# Step 3: Install IB Gateway & Configure IBC compatibility
# =============================================================================
info "Checking for IB Gateway installation in ${USER_HOME}/Jts..."

GATEWAY_DIR="${USER_HOME}/Jts/ibgateway"
if [ ! -d "${GATEWAY_DIR}" ]; then
    info "IB Gateway not found. Downloading stable Linux 10.45 installer..."
    TEMP_DIR=$(mktemp -d)
    
    # Download official stable offline gateway bundle
    wget -q -O "${TEMP_DIR}/ibgateway-stable-standalone-linux-x64.sh" \
        "https://download2.interactivebrokers.com/installers/ibgateway/stable-standalone/ibgateway-stable-standalone-linux-x64.sh"
    
    chmod +x "${TEMP_DIR}/ibgateway-stable-standalone-linux-x64.sh"
    
    info "Running silent quiet installation to ${GATEWAY_DIR}..."
    "${TEMP_DIR}/ibgateway-stable-standalone-linux-x64.sh" -q -dir "${GATEWAY_DIR}"
    
    rm -rf "${TEMP_DIR}"
    success "IB Gateway successfully installed in quiet mode."
else
    warn "IB Gateway directory already exists. Skipping installer download."
fi

# IBC looks for jars inside ~/Jts/ibgateway/{VERSION}/jars/, but the installer places
# them at the root. We resolve this by creating a self-referential version directory symlink.
info "Configuring IBC-compatibility symlink for Version 10.45..."
mkdir -p "${GATEWAY_DIR}"
if [ ! -e "${GATEWAY_DIR}/10.45" ]; then
    ln -s "${GATEWAY_DIR}" "${GATEWAY_DIR}/10.45"
    success "Created version symlink: ${GATEWAY_DIR}/10.45 -> ${GATEWAY_DIR}"
else
    info "Version 10.45 symlink already exists. Skipping."
fi

# =============================================================================
# Step 4: Provision IBC
# =============================================================================
info "Downloading and unpacking IBC 3.19.0..."
IBC_ZIP="/opt/ibc/IBC-3.19.0.zip"

if [ ! -f "/opt/ibc/scripts/ibcstart.sh" ]; then
    sudo wget -q -O "${IBC_ZIP}" "https://github.com/IbcAlpha/IBC/releases/download/3.19.0/IBCLinux-3.19.0.zip"
    sudo unzip -q -o "${IBC_ZIP}" -d /opt/ibc
    sudo chmod +x /opt/ibc/scripts/*.sh
    sudo rm -f "${IBC_ZIP}"
    
    # Hand ownership back to active user after sudo unzip
    sudo chown -R "${ACTIVE_USER}:${ACTIVE_USER}" /opt/ibc
    success "IBC unpacked and scripts made executable."
else
    warn "IBC scripts already present. Skipping download."
fi

# Sync config.ini to the default location expected by IBC (~/ibc/config.ini)
info "Setting up local IBC config symlink..."
mkdir -p "${USER_HOME}/ibc"
if [ ! -e "${USER_HOME}/ibc/config.ini" ]; then
    # Provision a clean config template if none exists in the repository
    if [ -f "${PROJECT_DIR}/deploy/ibc/config.ini" ]; then
        cp "${PROJECT_DIR}/deploy/ibc/config.ini" /opt/ibc/config.ini
    else
        warn "deploy/ibc/config.ini not found in local repo, generating placeholder."
        touch /opt/ibc/config.ini
    fi
    ln -s /opt/ibc/config.ini "${USER_HOME}/ibc/config.ini"
    success "Symlinked ${USER_HOME}/ibc/config.ini -> /opt/ibc/config.ini"
else
    info "IBC local config symlink already exists."
fi

# =============================================================================
# Step 5: Environment File Scaffolding
# =============================================================================
info "Setting up credential environment file at /etc/cl-trader.env..."
ENV_FILE="/etc/cl-trader.env"

if [ ! -f "${ENV_FILE}" ]; then
    # Copy from package template
    if [ -f "${PROJECT_DIR}/deploy/systemd/cl-trader.env.template" ]; then
        sudo cp "${PROJECT_DIR}/deploy/systemd/cl-trader.env.template" "${ENV_FILE}"
    else
        # Write template manually if not in local repo
        sudo tee "${ENV_FILE}" > /dev/null <<EOF
# --- IBKR Credentials ---
TWS_USERID=your_username
TWS_PASSWORD=your_password
TRADING_MODE=paper

# --- IBC Configuration ---
IBC_GATEWAY_VERSION=10.45

# --- Application Paths ---
CL_DATA_ROOT=/opt/cl-trader/data

# --- API Keys ---
FRED_API_KEY=your_fred_key

# --- Telegram Notifications ---
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
EOF
    fi
    # Set strict secure permissions (chmod 600) so credentials are read-only for root
    sudo chmod 600 "${ENV_FILE}"
    success "Credential file initialized at ${ENV_FILE}. Set permissions to 600."
    warn "ACTION REQUIRED: Edit ${ENV_FILE} to populate real credentials before starting services."
else
    info "${ENV_FILE} already exists. Retaining current credentials."
fi

# =============================================================================
# Step 6: Systemd Service Interpolation & Deployment
# =============================================================================
info "Deploying and interpolating systemd service files..."

# Re-read paths dynamically from project repository
SYSTEMD_SRC_GATEWAY="${PROJECT_DIR}/deploy/systemd/ibc-gateway.service"
SYSTEMD_SRC_TRADER="${PROJECT_DIR}/deploy/systemd/live-trader.service"
LOGROTATE_SRC="${PROJECT_DIR}/deploy/systemd/cl-trader.logrotate"

# Perform dynamic string substitution to replace the hardcoded user /home/bwang008/ paths
# with the active cloud host's user and home directory
interpolate_and_deploy() {
    local src_file=$1
    local dest_file=$2
    info "Interpolating and copying $(basename "${src_file}") to ${dest_file}..."
    
    # We substitute /home/bwang008/ with the dynamic ${USER_HOME}/ and the user/group values
    sed -e "s|/home/bwang008|${USER_HOME}|g" \
        -e "s|User=bwang008|User=${ACTIVE_USER}|g" \
        -e "s|Group=bwang008|Group=${ACTIVE_USER}|g" \
        "${src_file}" | sudo tee "${dest_file}" > /dev/null
}

if [ -f "${SYSTEMD_SRC_GATEWAY}" ]; then
    interpolate_and_deploy "${SYSTEMD_SRC_GATEWAY}" "/etc/systemd/system/ibc-gateway.service"
else
    error "Missing source file ${SYSTEMD_SRC_GATEWAY}!"
fi

if [ -f "${SYSTEMD_SRC_TRADER}" ]; then
    interpolate_and_deploy "${SYSTEMD_SRC_TRADER}" "/etc/systemd/system/live-trader.service"
else
    error "Missing source file ${SYSTEMD_SRC_TRADER}!"
fi

# Deploy log rotation rules
if [ -f "${LOGROTATE_SRC}" ]; then
    sudo cp "${LOGROTATE_SRC}" "/etc/logrotate.d/cl-trader"
    sudo chmod 644 "/etc/logrotate.d/cl-trader"
    success "Logrotate configuration deployed."
else
    warn "Logrotate source config not found at ${LOGROTATE_SRC}."
fi

# Reload systemd system configuration
info "Reloading systemd daemon..."
sudo systemctl daemon-reload

# Auto-enable services on startup
sudo systemctl enable ibc-gateway.service
sudo systemctl enable live-trader.service
success "Systemd services successfully deployed, reloaded, and enabled."

# =============================================================================
# Step 7: Cloud VNC Setup Information
# =============================================================================
info "Configuring TightVNC server instructions..."

# Write out a helpful instruction file inside their config directory
mkdir -p "${USER_HOME}/.vnc"
success "VNC prep folder created at ${USER_HOME}/.vnc"

# =============================================================================
# Step 8: Execution Completion Summary
# =============================================================================
echo -e "\n============================================================================="
success "CL Analyst Headless Scaffolding Completed Successfully!"
echo -e "============================================================================="
echo -e "The setup script has completed the following layers:"
echo -e "  1. Installed Java, Xvfb, tightvncserver, socat, net-tools, logrotate."
echo -e "  2. Provisioned /opt/cl-trader/ and chowned to active user: ${ACTIVE_USER}"
echo -e "  3. Downloaded & installed stable IB Gateway to ${USER_HOME}/Jts/ibgateway/"
echo -e "  4. Set up IBC 3.19.0 in /opt/ibc/ with dynamic version/config symlinks."
echo -e "  5. Created /etc/cl-trader.env with strict secure permissions (600)."
echo -e "  6. Interpolated user paths in systemd services and loaded configurations."
echo -e "============================================================================="
echo -e "\n${YELLOW}[CRITICAL ACTION REQUIRED]${NC}"
echo -e "1. Populate your API secrets, keys, and credentials:"
echo -e "   $ sudo nano /etc/cl-trader.env"
echo -e "\n2. Cloud VPS Only (One-time EULA Acceptance):"
echo -e "   a) Start VNC Server to create a graphical session:"
echo -e "      $ tightvncserver :1"
echo -e "   b) SSH tunnel port 5901 from your local PC to the VPS, then open a VNC client."
echo -e "   c) Inside the VNC view, start IB Gateway manually to accept EULAs once:"
echo -e "      $ ${USER_HOME}/Jts/ibgateway/ibgateway"
echo -e "   d) Kill the manual session, stop VNC, and let systemd handle it from there!"
echo -e "\n3. Starting & Monitoring commands:"
echo -e "   - Start trader:    $ sudo systemctl start live-trader.service"
echo -e "   - Check status:    $ systemctl status live-trader.service"
echo -e "   - Stream logs:     $ journalctl -u live-trader.service -f"
echo -e "=============================================================================\n"
