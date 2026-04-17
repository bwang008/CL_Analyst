# Automated Smoke Test Workflow

This workflow executes the institutional-grade Automated Smoke Test to verify the health of the live trading architecture. It checks Database & Logging Integrity, Cache & Artifact Validation, and Train-Serve Parity.

## Usage
Trigger this workflow by asking the agent to run the `/smoketest` workflow.

## Steps

### 1. Execute the Pipeline Tests
Run the automated pipeline test natively in the activated environment.

// turbo
```powershell
conda run -n trader python tests/smoke_test_pipeline.py
conda run -n trader python tmp/playback_simulator.py
```

### 2. Verify Output Matrix
Review the terminal output:
- Ensure all 3 Stages pass ([PASS]).
- If any stage fails ([FAIL]), halt deployment operations immediately and alert the user to the specific failure point.
- The `tests/smoke_test_pipeline.py` script automatically appends the result to `reports/HEALTH_REPORT.txt`.

### 3. Report Results
Summarize the results of the test back to the user, highlighting any warnings or performance issues noted in the output.
