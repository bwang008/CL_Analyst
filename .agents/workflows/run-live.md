---
description: Start the live trader (dry-run or live mode)
---

// turbo-all

## Deploying Code Changes to the Live Trader

The live trader runs as a **systemd service** (`live-trader.service`) on WSL
with `Restart=always` and `RestartSec=60`. To deploy code changes, use this
pipeline — **no `sudo` required**:

```bash
# 1. Push from local Windows
git add -A
git commit -m "fix: description"
git push origin development

# 2. Pull on WSL
wsl bash -c "cd /home/bwang008/projects/CL_Analyst && git pull origin development"

# 3. Kill the trader — systemd auto-restarts in ~60s
wsl bash -c "pkill -f 'live_trader'"

# 4. Verify (wait ~60s)
wsl bash -c "systemctl status live-trader.service"
```

**Do NOT** start the trader manually with `nohup ... &` — this creates a
duplicate process that conflicts with the systemd-managed instance (IBKR
client ID collisions).

---

## Starting from Scratch (No systemd service yet)

1. Run diagnostics to verify IBKR connection and telemetry health:
   ```bash
   conda run -n trader python scripts/diagnose_telemetry.py
   ```

2. Start the live trader in dry-run mode using a production JSON config (default safe mode):
   ```bash
   conda run -n trader python -m src.live_execution.live_trader --config configs/strategies/hourly_ensemble_002.json --dry-run
   ```

If live mode is explicitly requested, use:
   ```bash
   conda run -n trader python -m src.live_execution.live_trader
   ```

3. Monitor the output for connection issues, bar updates, and signal generation.

4. **CRITICAL AGENT INSTRUCTION**: If you are running this as a test or dry-run, the process will run infinitely in the background. When you are finished auditing the output, you **MUST** explicitly terminate the background command using the `send_command_input` tool with `Terminate: true` to prevent orphaned instances consuming API resources.
