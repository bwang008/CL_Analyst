# Bare Metal Deployment — systemd Process Management

This directory contains systemd unit files for running the CL_Analyst
live trader and IBC (IB Gateway Controller) on a headless Linux VPS.

## Architecture

```
┌─────────────────────────────────────────────────────┐
│  Ubuntu VPS (e2-small on GCP)                       │
│                                                     │
│  ┌─────────────────────┐  ┌──────────────────────┐  │
│  │  ibc-gateway.service│  │ live-trader.service   │  │
│  │  (systemd)          │  │ (systemd)             │  │
│  │                     │  │                       │  │
│  │  IBC → IB Gateway   │  │ Python live_trader.py │  │
│  │  Xvfb (headless)    │  │ connects to :4002     │  │
│  │  Port 4002 (paper)  │  │                       │  │
│  └─────────────────────┘  └──────────────────────┘  │
│                                                     │
│  Cron: smoke_test_pipeline.py → Telegram            │
└─────────────────────────────────────────────────────┘
```

## Installation

```bash
# 1. Copy unit files
sudo cp deploy/systemd/ibc-gateway.service /etc/systemd/system/
sudo cp deploy/systemd/live-trader.service /etc/systemd/system/

# 2. Copy environment file (fill in your credentials!)
sudo cp deploy/systemd/cl-trader.env /etc/cl-trader.env
sudo chmod 600 /etc/cl-trader.env   # restrict permissions
sudo nano /etc/cl-trader.env         # fill in TWS_USERID, TWS_PASSWORD, etc.

# 3. Reload systemd and enable services
sudo systemctl daemon-reload
sudo systemctl enable ibc-gateway.service
sudo systemctl enable live-trader.service

# 4. Start the services
sudo systemctl start ibc-gateway.service
# Wait ~30s for gateway to initialize, then:
sudo systemctl start live-trader.service
```

## Management Commands

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

## Code Deployment (No sudo)

The `live-trader.service` is configured with `Restart=always` and
`RestartSec=60`.  This means **killing the Python process is enough** —
systemd will detect the exit and auto-restart the trader with the
updated code within 60 seconds.  No `sudo systemctl restart` required.

### Full pipeline: local Windows → WSL live trader

```bash
# 1. Commit and push from local Windows (PowerShell)
cd C:\Users\bwang\Documents\GitHub\CL_Analyst_Development
git add -A
git commit -m "fix: description of change"
git push origin development

# 2. Pull the changes on the WSL clone
wsl bash -c "cd /home/bwang008/projects/CL_Analyst && git pull origin development"

# 3. Kill the trader process — systemd auto-restarts it in ~60s
wsl bash -c "pkill -f 'live_trader'"
#   -f matches the full command line, so 'live_trader' targets the right process
#   No sudo needed: the process runs as bwang008 (same user)

# 4. Verify the restart (wait ~60s, then check)
wsl bash -c "systemctl status live-trader.service"
#   or check the log:
wsl bash -c "journalctl -u live-trader --since '2 min ago' --no-pager"
```

### Why this works

| Step | What happens |
|------|-------------|
| `pkill -f 'live_trader'` | Sends SIGTERM to the Python process (owned by `bwang008`, no sudo needed) |
| systemd detects exit | The service's `MainPID` is gone, systemd sees it as a failure |
| `Restart=always` fires | After `RestartSec=60`, systemd runs `ExecStart` again |
| `ExecStartPre` waits | Pre-flight confirms IB Gateway is listening on port 4002 |
| Trader starts fresh | Picks up the newly pulled code from the working directory |

> **Important**: Do NOT manually start the trader with `nohup ... &`
> alongside the systemd service.  This creates duplicate processes
> competing for the same IBKR client ID, causing connection failures.
> Always let systemd manage the lifecycle.

## Daily Smoke Test Cron

```bash
# Add to crontab (crontab -e):
0 8 * * * /opt/cl-trader/venv/bin/python /opt/cl-trader/app/tests/smoke_test_pipeline.py --telegram 2>&1 | tee -a /opt/cl-trader/logs/smoke_test.log
```
