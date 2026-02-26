---
description: Start the live trader (dry-run or live mode)
---

// turbo-all

1. Run diagnostics to verify IBKR connection and telemetry health:
   ```bash
   conda run -n trader python scripts/diagnose_telemetry.py
   ```

2. Start the live trader in dry-run mode (default safe mode):
   ```bash
   conda run -n trader python -m src.live_execution.live_trader --dry-run
   ```

If live mode is explicitly requested, use:
   ```bash
   conda run -n trader python -m src.live_execution.live_trader
   ```

3. Monitor the output for connection issues, bar updates, and signal generation.
