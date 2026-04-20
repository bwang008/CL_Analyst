---
description: Start the live trader (dry-run or live mode)
---

// turbo-all

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
