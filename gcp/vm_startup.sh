#!/bin/bash
# =============================================================================
# GCP VM Startup Script
# Runs automatically when the VM first boots. Installs Python + ML packages.
# Logs to /tmp/startup.log, signals completion via /tmp/startup_done.
# =============================================================================

set -e
exec > /tmp/startup.log 2>&1

echo "$(date): === VM Startup Script ==="
echo "$(date): Installing system packages..."

# System packages
apt-get update -qq
apt-get install -y -qq software-properties-common
add-apt-repository -y ppa:deadsnakes/ppa
apt-get update -qq
apt-get install -y -qq python3.12 python3.12-venv python3.12-dev tmux unzip libgomp1 > /dev/null

echo "$(date): Creating Python virtual environment..."

# Create Python virtual environment
python3.12 -m venv /opt/optuna-env
source /opt/optuna-env/bin/activate

echo "$(date): Installing Python packages..."

# Install ML packages
pip install --no-cache-dir --quiet \
    'lightgbm>=4.0.0' \
    'optuna>=3.0.0' \
    'pandas>=1.5.0' \
    'numpy>=1.21.0' \
    'scikit-learn>=1.0.0' \
    'pyarrow>=10.0.0' \
    'sqlalchemy>=1.4.0' \
    'tabulate>=0.9.0' \
    'pandas-ta==0.4.71b0' \
    'python-dotenv>=1.0.0'

# Make venv accessible to all users (SSH user needs write access)
chmod -R 777 /opt/optuna-env

# Signal completion
touch /tmp/startup_done
echo "$(date): === Startup Complete ==="
