---
description: Run the full test suite with auto-approved commands
---

// turbo-all

1. Run all tests (excluding slow tests):
   ```powershell
   $env:PYTHONUTF8 = "1"
   conda run -n trader python -m pytest tests/ -v --tb=short -m "not slow"
   ```

2. Verify hourly pipeline parity (production-critical — all active strategies use 1h bars):
   ```powershell
   conda run -n trader python -m pytest tests/test_pipeline_parity_hourly.py -v --tb=long
   ```

3. If any tests fail, analyze the failures and attempt to fix them.

4. Re-run the failing tests to verify the fix:
   ```bash
   conda run -n trader python -m pytest tests/<failing_test_file>.py -v --tb=long
   ```

5. Once all tests pass, summarize the results.
