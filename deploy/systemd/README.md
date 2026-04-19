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

## Daily Smoke Test Cron

```bash
# Add to crontab (crontab -e):
0 8 * * * /opt/cl-trader/venv/bin/python /opt/cl-trader/app/tests/smoke_test_pipeline.py --telegram 2>&1 | tee -a /opt/cl-trader/logs/smoke_test.log
```
