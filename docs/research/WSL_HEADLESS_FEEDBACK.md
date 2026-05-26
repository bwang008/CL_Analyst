This is an exceptionally well-thought-out deployment plan. Your breakdown into discrete phases, the isolation of concerns (using a dedicated service user), and your comprehensive verification checklist demonstrate a mature, systems-engineering approach. You are clearly building this to be robust.

However, as a principal engineer reviewing this for a transition from a local WSL sandbox to a production-grade cloud Linux VPS, I see a few brittle points—particularly around process synchronization, environment management, and long-term stability—that we need to harden now so they don't cause catastrophic failures in production.

Here is my architectural review, highlighting potential traps and providing superior alternatives to ensure your setup is truly "headless" and "self-healing."

---

### 1. The Startup Synchronization Trap (Phase 7)

**The Flaw:** In Phase 7, you start the gateway, execute a `sleep 30`, and then start the live trader. This is a classic race condition anti-pattern. If IBKR's servers are slow, or IBC takes 35 seconds to bypass a prompt, your trader will start, fail to connect to `4002`, and crash. If it crashes too fast, systemd's burst limit might prevent it from restarting.
**The Fix:** systemd should handle this natively using a readiness check. We can use `netcat` (`nc`) to poll the port before systemd is allowed to start the Python process.

**Implementation:**
Modify `live-trader.service` to include `Requires`, `After`, and an `ExecStartPre` socket check:

```ini
[Unit]
Description=CL Analyst Live Trader
Requires=ibc-gateway.service
After=ibc-gateway.service

[Service]
# Wait until port 4002 is actively listening before launching the Python script
ExecStartPre=/bin/bash -c 'while ! nc -z 127.0.0.1 4002; do sleep 2; echo "Waiting for IB Gateway..."; done'

# Python execution (see point 2 below)
...

```

### 2. Conda and Systemd (Phase 5 & 7)

**The Flaw:** Running `conda activate trader` inside a systemd service script is notoriously flaky because `conda activate` relies on shell environment hooks that don't exist in systemd's non-interactive runtime.
**The Fix:** Bypass the activation wrapper entirely. When you use the absolute path to the Python interpreter *inside* the Conda environment, Conda automatically resolves the correct environment libraries.

**Implementation:**
In your `live-trader.service`, define `ExecStart` like this:

```ini
[Service]
WorkingDirectory=/opt/cl-trader/CL_Analyst
# Point DIRECTLY to the conda environment's python binary
ExecStart=/home/bwang008/miniconda3/envs/trader/bin/python src/live_execution/cli.py --config configs/strategies/live.json
Restart=always
RestartSec=10

```

### 3. IBC vs. Manual GUI Configuration (Phase 3 & 8)

**The Flaw:** In Phase 8, you mention using WSLg to manually configure the API port to `4002` and enable ActiveX/Socket clients.
**The Fix:** The entire point of IBC (IB Controller) is to avoid this. IBC injects these settings directly into IB Gateway's `jts.ini` file on startup. If you configure Phase 3 correctly, Phase 8 is largely unnecessary for paper accounts.

Ensure these are set in `/opt/ibc/config.ini`:

```ini
TradingMode=paper
OverrideTwsApiPort=4002
AcceptIncomingConnectionAction=accept
AcceptNonBrokerageAccountWarning=yes

```

*Note:* You still might need the GUI *once* if you haven't accepted the IBKR market data agreements on that specific user account, but the API settings should be handled by IBC.

### 4. Xvfb Execution (Phase 1 & 7)

**The Flaw:** The plan mentions installing `xvfb`, but doesn't explicitly state how it wraps the gateway. If IBC is launched without a display attached, Java Swing will throw a `HeadlessException` and crash.
**The Fix:** Ensure your `ibc-gateway.service` uses `xvfb-run`. This creates a temporary X11 display, runs the command, and destroys the display when the process dies.

**Implementation (`ibc-gateway.service`):**

```ini
[Service]
Environment="DISPLAY=:99"
# xvfb-run automatically handles the virtual framebuffer
ExecStart=/usr/bin/xvfb-run --auto-servernum --server-args="-screen 0 1024x768x24" /opt/ibc/scripts/ibcstart.sh -g -version 10.23

```

### 5. Log Exhaustion (Missing Phase)

**The Flaw:** A live trading bot running 24/5 generates massive logs. LightGBM inference logs, `ib_insync` tick data, and IB Gateway's internal Java logs will quietly fill up a cloud VM's disk, eventually causing the OS to lock up.
**The Fix:** You must implement Log Rotation from day one.

**Implementation:**
Add a step to configure `logrotate`. Create `/etc/logrotate.d/cl-trader`:

```text
/opt/cl-trader/logs/*.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
}

```

### 6. Time Synchronization (Crucial for Quant Trading)

While WSL inherits time from the Windows host, when you move to a Linux VPS, time drift is a silent killer for ML-based trading strategies (especially those relying on 5-minute/1-hour bars and rolling features).

* **Addition to Plan:** Add a prerequisite to install and enable `chrony` or `systemd-timesyncd` in your Linux environment to ensure NTP synchronization. If the clock drifts by even 10 seconds, your bar timestamps will misalign with IBKR's server time.

### 7. Graceful Shutdown (Systemd)

When systemd stops a service (e.g., during a deployment update), it sends `SIGTERM`. Does your `live_trader.py` catch `SIGTERM` to safely cancel pending bracket orders or close database connections?
Ensure your Python code traps this:

```python
import signal
import sys

def handle_sigterm(signum, frame):
    logger.info("SIGTERM received. Canceling pending orders and shutting down...")
    # Clean up logic here
    sys.exit(0)

signal.signal(signal.SIGTERM, handle_sigterm)

```

In `live-trader.service`, add `TimeoutStopSec=30` to give your script 30 seconds to clean up before systemd sends a hard `SIGKILL`.

---

### Summary of Feedback to the Agent

You should instruct your agent to update the plan with the following directives:

1. **Remove manual `sleep` commands** in favor of `ExecStartPre` socket polling (`nc -z`) in the systemd configuration.
2. **Define Conda python paths absolutely** in the systemd `ExecStart`, entirely bypassing `conda activate`.
3. **Ensure `xvfb-run` wraps the IBC startup script** in `ibc-gateway.service`.
4. **Add a log rotation step** to prevent disk exhaustion.
5. **Lean completely on IBC** for configuring the API port; minimize the manual GUI steps in Phase 8 to purely accepting legal agreements if necessary.

Your foundational approach is excellent. Implementing these refinements shifts the architecture from "a script that runs in the background" to a true **resilient daemon** capable of surviving network drops, application crashes, and unattended cloud reboots. Keep building.