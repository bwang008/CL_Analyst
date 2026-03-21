---
description: Run system diagnostics and health checks
---

// turbo-all

1. Run the data health diagnostic (cache, features, rollover, backups):
   ```bash
   conda run -n trader python scripts/diagnose_data_health.py --telemetry --verbose
   ```

2. Check the telemetry database:
   ```bash
   conda run -n trader python scripts/diagnose_telemetry.py
   ```

3. Verify the data schema:
   ```bash
   conda run -n trader python scripts/check_schema.py
   ```

4. Run a quick test to validate core components:
   ```bash
   conda run -n trader python -m pytest tests/test_telemetry.py tests/test_schema_contracts.py tests/test_live_features.py -v --tb=short
   ```

5. Summarize the health status of all systems. Pay special attention to:
   - Any FAIL items from `diagnose_data_health.py` — these indicate data corruption or feature drift
   - Signal Health: if all probabilities are below 0.55, suspect a rollover/cache issue
   - Feature Drift: if key features (ATR_14, DIST_ZSCORE, VOLFLOW) changed by >200%, the cache may have mixed back-adjustment bases
