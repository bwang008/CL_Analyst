# Validate Parity Workflow

This workflow runs the complete **Execution Parity Suite** — a multi-layer
validation that confirms the BacktestEngine, LiveTrader, and prediction
pipeline are producing identical results from the same inputs.

Run this workflow before deploying a new strategy config to live trading,
after modifying execution logic in `backtest_engine.py` or `live_trader.py`,
or as a periodic health check.

> **Scope note:** this workflow is the *offline* layer (config / feature / ATR /
> execution unit tests + shadow-log prediction parity). It does **not** reconcile
> the two engines at the trade-ledger level, so it can report green while the
> BacktestEngine and LiveTrader still disagree on cooldown/exit behavior. For
> trade-by-trade ledger reconciliation (a Parity-Mode livetest replay compared
> against the BacktestEngine ledger), run `/validate-ledger-parity` after this
> one is green.
>
> The suite is also **CL-fixtured** and does not exercise the session calendar,
> stale-bar watchdog, front-month roll, or live seed math (the T5 test pins are
> the only fence there) — a green run says nothing about a non-CL config's
> correctness.

## Usage

Trigger this workflow by asking the agent to run the `/validate-parity` workflow.

## Steps

### 1. Run the Parity Test Suite (pytest)

Run all parity-related unit tests. These are fast, offline, and require no
IBKR connection or telemetry database.

// turbo
```powershell
$env:PYTHONUTF8 = "1"
python -m pytest tests/test_config_parity.py tests/test_pipeline_parity.py tests/test_per_side_atr.py tests/test_execution_parity.py -v --tb=short
```

**What it validates:**
- `test_config_parity.py` — BacktestEngine and LiveTrader resolve identical
  parameter values (trailing_activation_mult, atr_period, max_hold_bars,
  trailing_atr_mult) from the same strategy JSON config.
- `test_pipeline_parity.py` — AlphaFactory produces identical feature values
  whether fed a full batch (training) or incrementally (live inference).
  If this fails, live predictions silently diverge from training.
- `test_per_side_atr.py` — Per-side ATR periods and trailing offsets are
  correctly routed to the appropriate trade side. Backward-compatible with
  configs that only have global ATR settings.
- `test_execution_parity.py` — Recovery bars_held uses the correct bar_size
  (not hardcoded 5-min), initial_sl_price is stored and preserved through
  trailing stop modifications, and DB migrations work for older schemas.

### 2. Run Prediction Parity Validation (shadow log replay)

Replay the production shadow log through offline model inference and compare
predicted probabilities.  Requires a shadow log file and model artifacts.

```powershell
$env:PYTHONUTF8 = "1"
python scripts/validate_parity.py
```

**Modes:**
- **Mode A (Feature Replay)**: Uses logged feature columns from the shadow log
  for offline inference. Validates feature computation determinism.
- **Mode B (Full Rebuild)**: Rebuilds features from OHLCV via AlphaFactory and
  compares both features AND predictions. Gold-standard test.

**Override defaults:**
```powershell
# Specific shadow log file + model
python scripts/validate_parity.py --file data/processed/live_shadow_log.parquet --model-dir models/registry/EXP-017_S_Ultimate

# Filter to specific strategy
python scripts/validate_parity.py --strategy ManateeKoala_Conservative

# Adjust tolerance (default: 1e-6)
python scripts/validate_parity.py --tolerance 1e-4
```

### 3. Run Config Parity Report (side-by-side comparison)

Generate a human-readable parameter comparison table for a specific production
strategy config. Useful for manual review before deployment.

```powershell
$env:PYTHONUTF8 = "1"
python tests/test_config_parity.py --compare configs/strategies/HS09_Ensemble_E01_06032026.json
```

### 4. Verify Output

Review the results from each step:

- **Step 1**: All tests should show `PASSED`. If any show `FAILED`, halt deployment
  and investigate the specific parameter mismatch or feature divergence.
- **Step 2**: Look for `[PASS] PIPELINE PARITY CONFIRMED`. If `[FAIL]` appears,
  the live pipeline is producing different predictions than offline inference —
  this is a critical bug that will cause silent performance degradation.
- **Step 3**: All shared parameters should show `OK` in the Match column.
  Parameters showing `XX` indicate a mismatch that must be fixed before deployment.

### 5. Report Results

Summarize the results back to the user:

| Layer | Tool | What It Catches |
|-------|------|-----------------|
| Config Parity | `test_config_parity.py` | Naming mismatches, missing keys, default divergence |
| Feature Parity | `test_pipeline_parity.py` | Training vs live feature computation drift |
| ATR Parity | `test_per_side_atr.py` | Per-side bracket sizing bugs |
| Execution Parity | `test_execution_parity.py` | Recovery bars_held time dilation, initial_sl_price schema |
| Prediction Parity | `validate_parity.py` | End-to-end model inference divergence |
