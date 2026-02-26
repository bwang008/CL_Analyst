---
description: Run the full test suite with auto-approved commands
---

// turbo-all

1. Run all tests (excluding slow tests):
   ```bash
   conda run -n trader python -m pytest tests/ -v --tb=short -m "not slow"
   ```

2. If any tests fail, analyze the failures and attempt to fix them.

3. Re-run the failing tests to verify the fix:
   ```bash
   conda run -n trader python -m pytest tests/<failing_test_file>.py -v --tb=long
   ```

4. Once all tests pass, summarize the results.
