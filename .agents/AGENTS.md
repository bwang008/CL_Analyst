# Global Agent Rules

## Pipeline Failures
If a backtest, data pipeline script, or sweep script produces **0 trades** or **0 pairs**, you must treat it as a FATAL pipeline failure requiring deep data inspection and root cause analysis. Do NOT treat "0 trades" as a mathematically valid or acceptable outcome of a restrictive strategy.

When investigating these failures, proactively verify:
1. **Dataset Integrity**: Does the data contain expected columns? Are there mismatched timestamps resulting in silent `NaN`s during merges (e.g., `pd.reindex()`)?
2. **Frequency Alignment**: Do the resolutions of the execution data and the signal data match, or are they being merged improperly?
3. **Guardrails**: Are filtering layers (like `ExecutionGuard`) actually blocking all trades, or are they a red herring for underlying data corruption?
