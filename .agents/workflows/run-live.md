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

## Preflight (startup hard-raises on each of these by design)

Before starting, verify — for the config's symbol `<SYM>`/`<sym>`:

1. **Config resolves** (fail-fast resolver, T1 — catches missing/unknown/mismatched `execution_symbol`):
   ```bash
   conda run -n trader python -c "import json; from src.live_execution.instrument_context import resolve_instrument_context; print(resolve_instrument_context(json.load(open('configs/strategies/<config>.json'))))"
   ```
2. **1h seed present:** `data/processed/<SYM>_raw_1h.parquet` (per-symbol live seed via
   `derive_data_paths`; missing seed raises at startup).
3. **Macro files present:** `fred_macro_data_<sym>.csv` + `cftc_cot_<sym>.csv` (missing file or
   missing vol column raises).
4. **Hourly-only symbols** (no 5m seed — all new symbols): the config must set
   `"live_config": {"enable_5m_stream": false, ...}` — the key defaults to `true` and startup then
   fails on the missing 5m seed.

## Starting from Scratch (No systemd service yet)

1. Run diagnostics to verify IBKR connection and telemetry health:
   ```bash
   conda run -n trader python scripts/diagnose_telemetry.py
   ```

2. Start the live trader in dry-run mode using a production JSON config (default safe mode):
   ```bash
   conda run -n trader python -m src.live_execution.cli --config configs/strategies/hourly_ensemble_002.json --dry-run
   ```

If live mode is explicitly requested, use:
   ```bash
   conda run -n trader python -m src.live_execution.cli --config configs/strategies/<config>.json
   ```

   (`python -m src.live_execution.cli` is the canonical multi-symbol entry point; the legacy
   `python -m src.live_execution.live_trader` module entry still exists, but use the CLI.)

3. Monitor the output for connection issues, bar updates, and signal generation.

4. **CRITICAL AGENT INSTRUCTION**: If you are running this as a test or dry-run, the process will run infinitely in the background. When you are finished auditing the output, you **MUST** explicitly terminate the background command using the `send_command_input` tool with `Terminate: true` to prevent orphaned instances consuming API resources.
