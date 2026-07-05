# Headless Standalone Deployment Guide

This guide documents how to deploy the CL Analyst live trading system as a fully
autonomous, headless service — the broker session and trading engine start, run,
and self-heal without human intervention.

> **Tested on:** WSL 2 / Ubuntu 22.04 with IB Gateway 10.45 Stable, IBC 3.19.0,
> Python 3.12 (Miniconda), systemd enabled.

---

## Prerequisites

| Component | Version | Purpose |
|---|---|---|
| Ubuntu 22.04+ | WSL 2 or bare-metal | Host OS |
| Java (OpenJDK 11+) | `default-jre` | IB Gateway runtime |
| Xvfb | `xvfb` | Virtual X11 framebuffer (headless GUI) |
| IB Gateway Stable | 10.45 (offline installer) | IBKR API session |
| IBC | 3.19.0 | Automates Gateway login / daily restarts |
| Miniconda 3 | latest | Python environment manager |
| Python | 3.12 | Runtime for the live trader |
| netcat (`nc`) | any | Socket pre-flight health checks |

Install OS packages:

```bash
sudo apt update && sudo apt install -y default-jre xvfb unzip wget curl socat net-tools
```

---

## Installation

### 1. IB Gateway (Offline Installer)

```bash
# Download the stable standalone offline installer
wget -O ~/Downloads/ibgateway-stable-standalone-linux-x64.sh \
    https://download2.interactivebrokers.com/installers/ibgateway/stable-standalone/ibgateway-stable-standalone-linux-x64.sh

# Install silently to ~/Jts/ibgateway/
chmod +x ~/Downloads/ibgateway-stable-standalone-linux-x64.sh
~/Downloads/ibgateway-stable-standalone-linux-x64.sh -q -dir ~/Jts/ibgateway

# Create version symlink for IBC compatibility
# Replace 10.45 with your installed version (check ~/Jts/ibgateway/jars/jts4launch-*.jar)
ln -sfn ~/Jts/ibgateway ~/Jts/ibgateway/10.45
```

### 2. IBC (IB Controller)

```bash
# Download IBC 3.19.0
wget -O ~/Downloads/IBCLinux-3.19.0.zip \
    https://github.com/IbcAlpha/IBC/releases/download/3.19.0/IBCLinux-3.19.0.zip

# Extract to /opt/ibc
sudo mkdir -p /opt/ibc
sudo unzip -o ~/Downloads/IBCLinux-3.19.0.zip -d /opt/ibc
sudo chmod +x /opt/ibc/scripts/*.sh
sudo chown -R $(whoami):$(whoami) /opt/ibc
```

### 3. IBC Configuration

Create the IBC config directory and symlink:

```bash
mkdir -p ~/ibc
ln -sfn /opt/ibc/config.ini ~/ibc/config.ini
```

Edit `/opt/ibc/config.ini` — key settings:

```ini
IbLoginId=                                    # leave blank (passed via env vars)
IbPassword=                                   # leave blank (passed via env vars)
TradingMode=paper
AcceptNonBrokerageAccountWarning=yes
ExistingSessionDetectedAction=primary
OverrideTwsApiPort=4002
AcceptIncomingConnectionAction=accept
```

### 4. Python Environment

```bash
# Create conda environment with Python 3.12
conda create -n trader python=3.12 -y

# Install dependencies
~/miniconda3/envs/trader/bin/pip install -r ~/projects/CL_Analyst/requirements.txt

# Verify
cd ~/projects/CL_Analyst
~/miniconda3/envs/trader/bin/python -m src.live_execution.live_trader --help
```

### 5. Data Directory

```bash
# Create the deployment directory structure
sudo mkdir -p /opt/cl-trader/logs
sudo chown -R $(whoami):$(whoami) /opt/cl-trader

# Symlink to shared data (WSL example — adjust path for cloud VMs)
ln -sfn /mnt/c/CL_Analyst_Data /opt/cl-trader/data
```

---

## Configuration

### Environment File

Copy the template and fill in your credentials:

```bash
sudo cp ~/projects/CL_Analyst/deploy/systemd/cl-trader.env /etc/cl-trader.env
sudo chmod 600 /etc/cl-trader.env
sudo nano /etc/cl-trader.env
```

Required values:

```ini
TWS_USERID=your_ibkr_paper_username
TWS_PASSWORD=your_ibkr_paper_password
TRADING_MODE=paper
IBC_GATEWAY_VERSION=10.45

CL_DATA_ROOT=/opt/cl-trader/data
FRED_API_KEY=your_fred_api_key

TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
```

> **Security:** The file is `chmod 600` and readable only by the owner.
> Never commit credentials to git.

### First-Time GUI Login

On the very first launch, you must complete a visual login to accept the IBKR
terms of service. This only needs to be done once.

**WSL (via WSLg):**
```bash
~/Jts/ibgateway/ibgateway
```
The IB Gateway window will appear on your Windows desktop. Log in with paper
credentials, then go to **Configure → Settings → API → Settings** and verify:
- Socket port is `4002`
- "Read-Only API" is **unchecked**

Close the gateway after configuration is saved.

**Cloud VM (via VNC):**
Install a VNC server (`tightvnc` or `x11vnc`), connect to the display, and
complete the same steps.

---

## Service Management

### Install Services

```bash
# Copy service files and logrotate config
sudo cp ~/projects/CL_Analyst/deploy/systemd/ibc-gateway.service /etc/systemd/system/
sudo cp ~/projects/CL_Analyst/deploy/systemd/live-trader.service /etc/systemd/system/
sudo cp ~/projects/CL_Analyst/deploy/systemd/cl-trader.logrotate /etc/logrotate.d/cl-trader

# Reload systemd and enable services
sudo systemctl daemon-reload
sudo systemctl enable ibc-gateway.service
sudo systemctl enable live-trader.service
```

### Start Services

```bash
# Start the gateway first
sudo systemctl start ibc-gateway.service

# The live trader service has a pre-flight check that waits for port 4002
# to become available (up to 120 seconds), so it can be started immediately:
sudo systemctl start live-trader.service
```

### Common Commands

```bash
# Check status
sudo systemctl status ibc-gateway
sudo systemctl status live-trader

# View logs (live tail)
journalctl -u ibc-gateway -f
journalctl -u live-trader -f

# Restart after config change
sudo systemctl restart live-trader

# Stop everything
sudo systemctl stop live-trader
sudo systemctl stop ibc-gateway
```

---

## Multi-Strategy Deployment (Fleet Runner)

`deploy/systemd/fleet-runner.service` is the **multi-strategy replacement for
`live-trader.service`**. Instead of one hardcoded `--config`, it runs
`python -m src.live_execution.fleet_runner --manifest configs/fleet/fleet_manifest.json`,
which validates the fleet (explicit `live_config.client_id` per config, ids
unique and spaced >= 2 apart, <= 16 enabled instances per gateway), then
launches one live-CLI child per enabled instance with a staggered start
(`stagger_seconds`) and restarts crashed children with capped backoff.
systemd restarts the runner; the runner restarts the children — the children
are not systemd units. Full details: `deploy/systemd/README.md`.

### Per-config prerequisites (the runner validates client_ids ONLY)

The fleet runner's manifest validation covers client_id spacing/uniqueness — it
does **not** validate the configs themselves. Each child runs
`python -m src.live_execution.cli`, which **fail-fasts at startup** via
`resolve_instrument_context` (`cli.py:227-229`). Per enabled config:

- **Truthful `execution_symbol`** — a missing/unknown/mismatched symbol makes
  the child raise before connecting to IBKR.
- **Per-symbol data artifacts on the host data root** — the 1h seed
  `{SYM}_raw_1h.parquet` (≥ 4,320 1h bars), `fred_macro_data_<sym>.csv`, and
  `cftc_cot_<sym>.csv`. A missing seed/macro file raises at startup, so the
  child **crash-loops under the runner's restart backoff** until the artifact
  is staged.
- **No 5m seed needed for hourly models** — a seedless symbol shallow-bootstraps
  its 5m window from IBKR on first run (loud SHALLOW 5M banner + Telegram Mode
  stamp) and warm-starts from the saved cache thereafter;
  `enable_5m_stream: false` is an explicit opt-out only.

### Migration runbook (WSL)

1. `git pull origin development` in `~/projects/CL_Analyst` (brings the fleet
   runner plus pending live-trader fixes).
2. `pip install -r requirements.txt` in the trader env if dependencies changed.
3. Install the unit:
   `sudo cp deploy/systemd/fleet-runner.service /etc/systemd/system/ && sudo systemctl daemon-reload`.
4. Disable the old single-strategy unit first (duplicate traders collide on
   IBKR client IDs): `sudo systemctl disable --now live-trader.service`
   (leave disabled, or `sudo rm /etc/systemd/system/live-trader.service` +
   `daemon-reload`).
5. Enable on boot and start:
   `sudo systemctl enable ibc-gateway.service fleet-runner.service && sudo systemctl start ibc-gateway.service fleet-runner.service`.
6. **Smoke test first**: run with a 1-instance manifest (HS14B) with
   `"extra_args": ["--dry-run"]`, verify clean startup in
   `journalctl -u fleet-runner -f`, then add a second instance and verify the
   staggered launch, then flip dry-run off.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Ubuntu 22.04 (WSL 2 or Cloud VM)                           │
│                                                             │
│  ┌──────────────────────────┐  ┌──────────────────────────┐ │
│  │  ibc-gateway.service     │  │  live-trader.service      │ │
│  │  (systemd)               │  │  (systemd)                │ │
│  │                          │  │                           │ │
│  │  xvfb-run wraps:         │  │  ExecStartPre:            │ │
│  │    IBC → IB Gateway      │  │    nc -z 127.0.0.1 4002   │ │
│  │    Auto display :99      │  │  ExecStart:               │ │
│  │    Port 4002 (paper)     │  │    conda python -m ...    │ │
│  │                          │  │    --config hourly_008    │ │
│  │  Restart=on-failure      │  │  Restart=always           │ │
│  │  RestartSec=30           │  │  RestartSec=60            │ │
│  └──────────────────────────┘  └──────────────────────────┘ │
│                                                             │
│  /etc/cl-trader.env ─── credentials, API keys, paths        │
│  /etc/logrotate.d/cl-trader ─── daily rotation, 14 days     │
│                                                             │
│  Data: /opt/cl-trader/data → /mnt/c/CL_Analyst_Data        │
└─────────────────────────────────────────────────────────────┘
```

### Self-Healing Behavior

| Component | Restart Policy | Delay | Burst Limit |
|---|---|---|---|
| IB Gateway | `on-failure` | 30s | 10 per 600s |
| Live Trader | `always` | 60s | 10 per 3600s |

The live trader also has internal reconnection logic (up to 5 attempts) and a
stale-bar watchdog that triggers auto-restart if no new bars arrive within the
expected interval.

---

## Monitoring

### Telegram Alerts

The live trader sends the following notifications:

| Message | Meaning |
|---|---|
| 🚀 LiveTrader Online | Startup complete, connected to IBKR |
| 💓 1-Hour Heartbeat | System is alive and processing bars |
| 📊 Signal: LONG/SHORT | Model generated a trade signal |
| ✅ Order Filled | Bracket order executed |
| ⚠️ Connection Lost | IBKR connection dropped (auto-reconnecting) |
| 🔄 Restarted | Auto-restart triggered |

### Health Checks

```bash
# Verify API port is listening
ss -tlnp | grep 4002

# Check gateway logs for errors
journalctl -u ibc-gateway -n 50 --no-pager

# Check trader logs for bar events
journalctl -u live-trader -n 50 --no-pager | grep "NEW BAR"

# Verify process is running
systemctl is-active ibc-gateway live-trader
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `can't find jars folder` | IBC version/path mismatch | Create symlink: `ln -sfn ~/Jts/ibgateway ~/Jts/ibgateway/{VERSION}` |
| `Unrecognized Username or Password` | Credentials not set | Edit `/etc/cl-trader.env` with real IBKR paper credentials |
| Port 4002 not listening | Gateway not started or crashed | `sudo systemctl restart ibc-gateway` |
| `Pacing violation` | Too many IBKR API requests | Built-in backoff; wait 10 minutes |
| `Session stolen by TWS Mobile` | Another device logged in | IBC config `ExistingSessionDetectedAction=primary` handles this |
| Live trader won't start | Gateway not ready | Pre-flight check waits up to 120s; check gateway status first |
| `StartLimitIntervalSec` warning | Ubuntu 22.04 systemd version | Harmless warning; functionality still works |
| `ModuleNotFoundError: src` | Wrong working directory | Verify `WorkingDirectory` in service file matches repo path |

---

## Cloud Migration Notes

To replicate this setup on a GCP/AWS Ubuntu VM:

1. **Static IP**: Request a static external IP for your VM. IBKR may require
   this for 2FA exemption on dedicated API accounts.
2. **Firewall**: No inbound ports needed — all connections are outbound to IBKR.
3. **Data transfer**: Copy seed CSVs, model PKLs, and processed parquets to
   `/opt/cl-trader/data/` (use `gsutil cp` or `scp`) — **per traded symbol**,
   this includes the `{SYM}_raw_1h.parquet` live seed and the
   `fred_macro_data_<sym>.csv` / `cftc_cot_<sym>.csv` macro files (startup
   hard-raises on each missing artifact).
4. **First-time login**: Install VNC server (`tightvnc`), SSH tunnel to port
   5901, and complete the GUI login remotely.
5. **Systemd**: Works natively on cloud VMs (no WSL `wsl.conf` step needed).
6. **Monitoring**: Telegram alerts work from any network. Consider adding
   Prometheus/Grafana for metrics if running multiple strategies.
