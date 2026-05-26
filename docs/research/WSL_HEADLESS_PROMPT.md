# Headless Standalone Live Trader Deployment — WSL Setup Task

## Context & Background

This project is a quantitative crude oil (CL futures) trading system that runs a live execution engine (`src/live_execution/live_trader.py`) connected to Interactive Brokers (IBKR) via the `ib_insync` Python library. The engine subscribes to 5-minute and 1-hour bars from IBKR, runs ML model inference (LightGBM), and executes bracket orders on CL/MCL futures contracts.

Currently, the user manually starts IB Gateway (the IBKR API session) on their Windows desktop and then runs the Python trading script from a Windows terminal. **The goal is to make the entire system fully autonomous and headless** — the broker session and the trading engine should start, run, and self-heal without any human intervention — so it can eventually be deployed to a cloud Linux VPS.

### Repository Layout (Key Files)

- **`src/live_execution/live_trader.py`** — Main 3,300-line event-driven trading engine. Already has reconnection logic, auto-restart (up to 5 attempts), stale bar watchdog, Telegram heartbeat alerts, and graceful shutdown handlers.
- **`src/live_execution/cli.py`** — CLI entry point with argument parsing, strategy resolution, and per-client-id isolation.
- **`src/live_execution/ibkr_client.py`** — IBKR connection manager. Connects to `127.0.0.1:4002` (IB Gateway paper) with fallback to `7497` (TWS paper). Handles client ID conflicts, pacing violations, and contract qualification.
- **`deploy/systemd/`** — Pre-existing systemd service files (partially configured):
  - `ibc-gateway.service` — Launches IBC-managed IB Gateway with Xvfb.
  - `live-trader.service` — Runs the Python live trader with `Restart=always`.
  - `cl-trader.env` — Environment variable template (IBKR credentials, API keys, paths).
  - `README.md` — Existing deployment guide (needs updating after this work).
- **`configs/strategies/`** — JSON strategy configuration files.
- **`.env`** — Project-level environment variables (CL_DATA_ROOT, Telegram tokens, etc.).

### Current Environment

- **WSL Distribution**: Ubuntu-22.04 (WSL 2)
- **WSL Repository**: `~/projects/CL_Analyst` — cloned from GitHub, on `main` branch, clean and up-to-date.
- **Python**: Miniconda3 is already installed at `~/miniconda3`. A conda environment named `trader` may or may not exist yet inside WSL (verify and create if needed).
- **Windows Host**: The user develops code on Windows (`C:\Users\bwang\Documents\GitHub\CL_Analyst_Development` on the `development` branch). Code changes are pushed to GitHub and pulled into the WSL clone.

---

## Objective

Set up a fully standalone, headless deployment of the CL Analyst live trading system inside the WSL instance that:

1. **Automatically starts IB Gateway** (via IBC) without any GUI interaction.
2. **Automatically starts the Python live trader** after the gateway is ready.
3. **Self-heals** — both the gateway and trader restart automatically on crashes.
4. **Runs headlessly** — no monitor, no GUI clicks, no human involvement after initial setup.
5. **Can be reproduced** on a fresh Linux VM (GCP, AWS, etc.) using only the repository + a README.

---

## Tasks

### Phase 1: System Prerequisites

Install all OS-level dependencies required for headless IB Gateway operation:

1. **Java Runtime** — IB Gateway is a Java Swing application.
   ```bash
   sudo apt update && sudo apt install -y default-jre
   ```

2. **Virtual Frame Buffer (Xvfb)** — IB Gateway requires an X11 display to launch, even when running headless. Xvfb simulates a display in memory.
   ```bash
   sudo apt install -y xvfb
   ```

3. **Additional utilities** (if not already present):
   ```bash
   sudo apt install -y unzip wget curl socat net-tools
   ```

### Phase 2: Install IB Gateway (Stable, Linux Offline Installer)

1. Download the latest **IB Gateway Stable** offline installer for Linux from:
   `https://www.interactivebrokers.com/en/trading/ibgateway-stable.php`
   
   > **Important**: Use the **offline/standalone** installer (`.sh` file), NOT the online installer. The offline installer bundles its own JRE and does not require network access during installation.

2. Run the installer:
   ```bash
   chmod +x ibgateway-stable-standalone-linux-x86_64.sh
   ./ibgateway-stable-standalone-linux-x86_64.sh -q -dir ~/Jts/ibgateway
   ```
   The `-q` flag runs in quiet/unattended mode. `-dir` specifies the install directory.

3. Verify the installation:
   ```bash
   ls ~/Jts/ibgateway/
   # Should contain ibgateway script, jars/, etc.
   ```

### Phase 3: Install IBC (IB Controller)

IBC is the open-source automation layer that handles login, 2FA bypass (paper accounts), and daily restart scheduling for IB Gateway.

1. Download the latest IBC release from GitHub:
   ```bash
   # Check https://github.com/IbcAlpha/IBC/releases for the latest version
   wget https://github.com/IbcAlpha/IBC/releases/download/3.19.0/IBCLinux-3.19.0.zip
   sudo mkdir -p /opt/ibc
   sudo unzip IBCLinux-3.19.0.zip -d /opt/ibc
   sudo chmod +x /opt/ibc/scripts/*.sh
   ```

2. Configure IBC — edit `/opt/ibc/config.ini`:
   - Set `IbLoginId` and `IbPassword` (or leave blank if using environment variables).
   - Set `TradingMode=paper` for paper trading.
   - Set `AcceptIncomingConnectionAction=accept` to allow API connections.
   - Set `ExistingSessionDetectedAction=primary` to take over existing sessions.
   - Set the API port configuration to `OverrideTwsApiPort=4002`.
   - Set `AcceptNonBrokerageAccountWarning=yes`.

3. **Important**: Verify the IBC startup script paths match your IB Gateway installation directory. The `ibcstart.sh` script needs to know where IB Gateway is installed (typically `~/Jts/ibgateway/` or the version-specific subdirectory).

### Phase 4: Create the `cltrader` Service User

For security isolation (matching the existing systemd configs):

```bash
sudo useradd -r -m -s /bin/bash cltrader
# Copy necessary files to cltrader's home, or adjust service files to run as your user
```

> **Alternative**: If running locally in WSL for testing, you can modify the service files to run as your own user (`bwang008`) instead of creating a dedicated `cltrader` user. The cloud deployment should use the dedicated user.

### Phase 5: Configure the Conda Environment in WSL

1. Verify or create the `trader` conda environment:
   ```bash
   conda env list  # Check if 'trader' exists
   ```

2. If it doesn't exist, create it from the repository's requirements:
   ```bash
   cd ~/projects/CL_Analyst
   conda create -n trader python=3.10 -y
   conda activate trader
   pip install -r requirements.txt
   ```

3. Verify the live trader can import without errors:
   ```bash
   conda run -n trader python -c "from src.live_execution.live_trader import LiveTrader; print('Import OK')"
   ```

### Phase 6: Configure Data Paths and Environment

1. Create the data directory structure:
   ```bash
   sudo mkdir -p /opt/cl-trader/data
   sudo mkdir -p /opt/cl-trader/data/raw
   sudo mkdir -p /opt/cl-trader/data/processed
   sudo mkdir -p /opt/cl-trader/data/logs
   sudo mkdir -p /opt/cl-trader/logs
   ```

2. Copy or symlink necessary data files (seed CSV, model PKLs, processed parquets) from the repository or from the Windows host via `/mnt/c/`:
   ```bash
   # Example: symlink to Windows data if using shared CL_DATA_ROOT
   ln -s /mnt/c/CL_Analyst_Data /opt/cl-trader/data
   ```

3. Set up the environment file at `/etc/cl-trader.env` using the template from `deploy/systemd/cl-trader.env`:
   ```bash
   sudo cp ~/projects/CL_Analyst/deploy/systemd/cl-trader.env /etc/cl-trader.env
   sudo chmod 600 /etc/cl-trader.env
   ```
   Fill in the actual values:
   - `TWS_USERID` and `TWS_PASSWORD` (paper trading credentials)
   - `CL_DATA_ROOT` path
   - `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` (for monitoring)
   - `FRED_API_KEY` (for macro features)

### Phase 7: Enable systemd and Deploy Services

1. **Ensure systemd is enabled in WSL**. Check `/etc/wsl.conf`:
   ```ini
   [boot]
   systemd=true
   ```
   If this file doesn't exist or doesn't have this setting, create/edit it, then restart WSL from Windows:
   ```powershell
   wsl --shutdown
   ```
   Re-enter WSL after a few seconds.

2. **Verify systemd is running**:
   ```bash
   systemctl --version
   systemctl list-units --type=service | head
   ```

3. **Review and update the existing service files** in `deploy/systemd/` as needed:
   - **`ibc-gateway.service`**: Verify the `ExecStart` path matches where IBC and IB Gateway are installed. Verify the `User`/`Group` match the user you're running as. Update `IBC_GATEWAY_VERSION` to match the installed version.
   - **`live-trader.service`**: Verify the `ExecStart` path uses the correct Python/conda path inside WSL. Verify the `WorkingDirectory` points to the WSL repository clone. Verify the strategy config path is correct.

4. **Install and start the services**:
   ```bash
   sudo cp ~/projects/CL_Analyst/deploy/systemd/ibc-gateway.service /etc/systemd/system/
   sudo cp ~/projects/CL_Analyst/deploy/systemd/live-trader.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable ibc-gateway.service
   sudo systemctl enable live-trader.service
   sudo systemctl start ibc-gateway.service
   # Wait ~30 seconds for gateway to initialize
   sleep 30
   sudo systemctl start live-trader.service
   ```

### Phase 8: First-Time GUI Login (If Required)

On the very first launch, IB Gateway may require visual interaction for:
- Accepting the IBKR license agreement
- Completing 2FA (live accounts only; paper accounts skip this)
- Confirming API configuration settings

Because WSL 2 on Windows 10/11 supports **WSLg** (GUI forwarding), the IB Gateway window will automatically appear on the Windows desktop when launched from WSL. Use this to:
1. Complete any first-time login prompts.
2. Go to **Configure → Settings → API → Settings** and ensure:
   - "Enable ActiveX and Socket Clients" is checked.
   - Socket port is `4002`.
   - "Read-Only API" is unchecked (for live execution) or checked (for dry-run testing).
3. Once configured, close the gateway and restart it via the systemd service to verify headless operation.

---

## Verification Checklist

After completing all phases, verify each of these pass:

- [ ] **IB Gateway starts headlessly**: `sudo systemctl start ibc-gateway` succeeds; `systemctl status ibc-gateway` shows `active (running)`.
- [ ] **API port is listening**: `ss -tlnp | grep 4002` shows the gateway listening.
- [ ] **Live trader connects**: `sudo systemctl start live-trader` succeeds; `journalctl -u live-trader -n 50` shows "Connected to IBKR" and "Qualified CL continuous contract".
- [ ] **Warm-start completes**: Logs show seed CSV loading, IBKR backfill, and rolling DataFrame construction.
- [ ] **Bar subscription active**: Logs show `[NEW BAR]` events arriving every 5 minutes (during market hours) or a heartbeat during off-hours.
- [ ] **Telegram heartbeat received**: A "🚀 LiveTrader Online" message arrives in the configured Telegram chat, followed by hourly "💓 1-Hour Heartbeat" updates.
- [ ] **Self-healing works**: Kill the live-trader process (`sudo kill $(pgrep -f live_trader)`) and verify systemd restarts it within 60 seconds.
- [ ] **Gateway recovery works**: Kill the IB Gateway process and verify IBC restarts it, followed by the live trader reconnecting.
- [ ] **Dry-run mode works**: Running with `--dry-run` generates signals but does NOT place any orders (verify in IBKR paper account activity).

---

## Deliverable: README Documentation

After all tasks are complete and verified, add a new section to `README.md` (or create a standalone `docs/headless-deployment.md`) documenting:

### Section: Headless Standalone Deployment

1. **Prerequisites** — OS packages, Java, Xvfb, IBC version, IB Gateway version.
2. **Installation Steps** — Condensed, copy-paste-ready commands for a fresh Ubuntu 22.04 instance (WSL or cloud VM).
3. **Configuration** — What to put in `/etc/cl-trader.env`, IBC `config.ini` key settings, and any systemd service file customizations.
4. **First-Time Setup** — How to complete the initial GUI login (WSLg or VNC for cloud).
5. **Service Management** — `systemctl start/stop/restart/status` commands, log tailing with `journalctl`.
6. **Monitoring** — How Telegram alerts work, what each message type means, how to check system health.
7. **Troubleshooting** — Common failure modes and fixes (API port not listening, pacing violations, session stolen by TWS Mobile, daily restart timing).
8. **Cloud Migration** — Brief notes on how to replicate this setup on a GCP/AWS VM (static IP for 2FA exemption, firewall rules, etc.).

---

## Important Constraints

- **Do NOT modify any Python source code** in `src/` unless a bug is discovered during deployment. This task is purely infrastructure/operations.
- **Paper trading mode only** for all testing. Never connect to a live trading port (`4001`/`7496`).
- **Preserve existing systemd files** in `deploy/systemd/` as templates. If modifications are needed for the local WSL environment, make copies or update them in a backward-compatible way.
- **The WSL repository is at `~/projects/CL_Analyst`** on the `main` branch. Do not switch branches.
- **The Windows data root** is at `C:\CL_Analyst_Data` (accessible inside WSL via `/mnt/c/CL_Analyst_Data`). This contains the seed CSVs, model PKLs, and processed parquets needed for warm-start.