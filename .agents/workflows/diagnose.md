---
description: Run system diagnostics and health checks
---

// turbo-all

1. Check the telemetry database:
   ```bash
   conda run -n trader python scripts/diagnose_telemetry.py
   ```

2. Verify the data schema:
   ```bash
   conda run -n trader python scripts/check_schema.py
   ```

3. Run a quick test to validate core components:
   ```bash
   conda run -n trader python -m pytest tests/test_telemetry.py tests/test_schema_contracts.py tests/test_live_features.py -v --tb=short
   ```

4. Summarize the health status of all systems.
