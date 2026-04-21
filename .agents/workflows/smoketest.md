# Automated Smoke Test Workflow

This workflow executes the institutional-grade Automated Smoke Test to verify the health of the live trading architecture. It checks Database & Logging Integrity, Cache & Artifact Validation, and Train-Serve Parity.

It now also validates `warm_start_cache*.parquet` cadence based on filename:
- `warm_start_cache.parquet` -> 5-minute bars
- `warm_start_cache_1h.parquet` -> 1-hour bars
- `warm_start_cache_2h.parquet` -> 2-hour bars
- `warm_start_cache_4h.parquet` -> 4-hour bars

## Usage
Trigger this workflow by asking the agent to run the `/smoketest` workflow.

## Steps

### 0. Tier-0 Clone Checks (no telemetry required)
Run lightweight checks to validate `.env`, `CL_DATA_ROOT`, seed/macro data,
and the strategy config before running the full smoke test.

```bash
python scripts/tier0_checks.py --config configs/strategies/hourly_ensemble_004.json
```

### 1. Execute the Pipeline Tests
Run the automated pipeline test natively in the activated environment.

// turbo
```powershell
# Prevent conda from hanging on its interactive error-report prompt.
# 'report_errors false' is a one-time conda config (already set on this machine).
# PYTHONUTF8=1 prevents the UnicodeEncodeError that triggers the prompt.
$env:PYTHONUTF8 = "1"
conda run -n trader python tests/smoke_test_pipeline.py
$env:PYTHONUTF8 = "1"
conda run -n trader python tmp/playback_simulator.py
```

### 2. Verify Output Matrix
Review the terminal output:
- Ensure all 3 Stages pass ([PASS]).
- If any stage fails ([FAIL]), halt deployment operations immediately and alert the user to the specific failure point.
- The `tests/smoke_test_pipeline.py` script automatically appends the result to `reports/HEALTH_REPORT.txt`.
- Ensure `CACHE_STEP_*` checks are `PASS` for all discovered warm-start caches.

### 3. Report Results
Summarize the results of the test back to the user, highlighting any warnings or performance issues noted in the output.
