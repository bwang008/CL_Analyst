# Validate Parity Workflow

This workflow runs the complete **Execution Parity Suite** — a multi-layer
validation that confirms the BacktestEngine, LiveTrader, and prediction
pipeline are producing identical results from the same inputs, and that the
deployed fleet configs are actually exercising their models.

Run this workflow before deploying a new strategy config to live trading,
after modifying execution logic in `backtest_engine.py` or `live_trader.py`,
after any dataset/seed/roll migration, or as a periodic health check.

> **Scope note:** this workflow is the *offline* layer. It does **not** reconcile
> the two engines at the trade-ledger level, so it can report green while the
> BacktestEngine and LiveTrader still disagree on cooldown/exit behavior. For
> trade-by-trade ledger reconciliation (a Parity-Mode livetest replay compared
> against the BacktestEngine ledger), run `/validate-ledger-parity` after this
> one is green. For a post-mortem reconciliation of *actual live trades* against
> a backtest replay of the same window, adapt `scripts/run_reconciliation_audit.py`
> (its window/trade targets are hardcoded per investigation — it is a template,
> not a suite step).
>
> Environment: run everything in the **`trader` conda env** (global Python lacks
> pytest/lightgbm/pandas_ta). PowerShell: `conda run -n trader python …`.

## Usage

Trigger this workflow by asking the agent to run the `/validate-parity` workflow.

The deployed-config steps (0, 4, 5, 6) iterate over the **enabled** entries in
`configs/fleet/fleet_manifest.json` — that file is the source of truth for what
is live.

## Steps

### 0. Roll-Basis Gate (live data basis)

The Jul-2026 basis incident (`jit-roll-ratio-empty` ticket): live inference ran on
raw prices while models were trained ratio-adjusted, because `roll_history` was
empty in every roll-metadata file. This gate makes that impossible to miss.

// turbo
```bash
# Git Bash (stdlib only — any python works)
python - <<'PY'
import json, glob, os, sys
bad = []
for f in glob.glob(r'C:\CL_Analyst_Data\data\processed\.roll_metadata*.json'):
    if '_backup_' in f: continue
    d = json.load(open(f))
    n = len(d.get('roll_history', []))
    pend = d.get('pending_roll')
    print(f"{os.path.basename(f)}: rolls={n} cum_ratio={d.get('cumulative_ratio')} pending={bool(pend)}")
    if n == 0: bad.append(f)
    if pend: bad.append(f + ' (unresolved pending roll)')
sys.exit(1 if bad else 0)
PY
```

**PASS**: every fleet symbol's metadata has `roll_history` length > 0 and no
unresolved `pending_roll`. **FAIL** (empty history) = live features are on the raw
basis → run `scripts/backfill_roll_history.py` (dry-run first) per the ticket
checklist. **Remember:** corrected metadata only takes effect when each fleet
child is **restarted**.

Also gate on config encoding: `load_strategy_config` uses plain `json.load`,
which **crashes on a UTF-8 BOM** (PowerShell `Out-File` writes one by default —
this bit `ES02B` on 2026-07-11). Any BOM = the child dies at launch:

// turbo
```bash
python - <<'PY'
import json, sys
fleet = json.load(open('configs/fleet/fleet_manifest.json', encoding='utf-8-sig'))
bad = [i['config'] for i in fleet['instances'] if i.get('enabled')
       and open(i['config'],'rb').read(3) == b'\xef\xbb\xbf']
print('BOM check:', 'FAIL ' + ', '.join(bad) if bad else 'PASS')
sys.exit(1 if bad else 0)
PY
```
Fix: rewrite the file without the leading 3 bytes (verify `json.load` equality
before/after).

### 1. Run the Parity Test Suite (pytest)

Fast, offline, no IBKR connection or telemetry DB required.

// turbo
```powershell
$env:PYTHONUTF8 = "1"
conda run -n trader python -m pytest tests/test_config_parity.py tests/test_feature_parity.py tests/test_pipeline_parity_hourly.py tests/test_per_side_atr.py tests/test_execution_parity.py tests/test_parity_sltp_fill_basis.py tests/test_parity_cooldown_single_authority.py tests/test_optimizer_parity.py -v --tb=short
```

**What it validates:**
- `test_config_parity.py` — BacktestEngine and LiveTrader resolve identical
  parameter values from the same strategy JSON config.
- `test_feature_parity.py` / `test_pipeline_parity_hourly.py` — AlphaFactory
  produces identical feature values whether fed a full batch (training) or
  incrementally (live inference). If these fail, live predictions silently
  diverge from training.
- `test_per_side_atr.py` — per-side ATR periods and trailing offsets are routed
  to the correct trade side.
- `test_execution_parity.py` — recovery bars_held uses the correct bar_size,
  initial_sl_price survives trailing-stop modifications, DB migrations work.
- `test_parity_sltp_fill_basis.py` — SL/TP fills computed on the same price
  basis in both engines.
- `test_parity_cooldown_single_authority.py` — cooldown state has a single
  authority (no double-counting between engines).
- `test_optimizer_parity.py` — optimizer-side parity invariants.

### 2. Feature Parity (backtester vs livetest pipelines)

Covered by `test_feature_parity.py` and `test_pipeline_parity_hourly.py` in
Step 1 (full-batch AlphaFactory vs incremental `build_live_features` on the
same bars). Any diverging feature is a silent live-vs-training drift — treat
as deploy-blocking.

For a deeper per-feature diff at a specific timestamp there is
`scripts/feature_parity_compare.py`, but it is an **investigation template
with hardcoded constants** (`PARQUET_PATH`/`CONFIG_PATH`/`TARGET_TIMESTAMP`
still point at the June-2026 HS11 probe) — re-point those constants before
trusting its output; it is not runnable as-is.

### 3. Prediction Parity (CSV predictions vs live model.predict)

For **each enabled fleet config**, compare the batch prediction CSV (what the
backtest scored) against `model.predict()` on locally computed features:

```powershell
conda run -n trader python scripts/prediction_parity_compare.py --config configs/strategies/<CFG>.json --data data/processed/<SYM>_HourSet_<XX>.parquet --start-date 2026-01-01
```

**Gotchas:**
- The script reads `predictions_path` from the config. Batch folders get renamed
  (`batch_X` → `batch_X_SUFFIX`), leaving configs stale. The comparison is only
  valid against the CSV from the **same batch/sweep timestamp as `model_path`**
  — never substitute a same-named CSV from a different batch (different seed ⇒
  false FAIL, same-direction bias ⇒ false PASS). If the path is stale, fix it to
  the exact-batch CSV (or run against a temp copy of the config).
- `--start-date` bounds the (slow) vectorized feature build; recent months are
  what matter for live parity.
- **Known measurement artifact (verified 2026-07-11):** the script's feature
  recompute covers only what AlphaFactory can build from OHLCV+FRED — it omits
  the COT merge (and ~42 of the model's features for ES02B), NaN-filling them.
  That alone produces mean-|Δ| ~0.008 and ~100 signal flips per 3k bars with
  ZERO real divergence. The clean truth check is: `sigmoid(model.predict(stored
  parquet features))` reproduces the batch CSVs **exactly** (proven ES02B
  26,519 rows, |Δ|=0.0). Interpret this step's output only after subtracting
  the missing-feature artifact (watch for its "N model features missing"
  warning). Genuine recompute drift found so far: ADX/DMI family only, with
  negligible prob impact (mean 1e-6, 0 flips).

### 3b. Live shadow re-score (Mode A — the true live leg)

Re-score the deployed pickles on the exact `features_json` rows the live
children logged in `fleet_telemetry.db::shadow_log`, and compare against the
logged `prob_buy`/`prob_sell`. This validates live inference integrity
end-to-end (features → model → prob) with no recompute involved. Pattern:
load rows per symbol, `X = DataFrame(features).reindex(columns=booster.
feature_name()).astype(float32)`, `sigmoid(booster.predict(X))`, diff vs
logged probs.

**PASS**: |Δ| ≈ 0 (CL/SI/GC re-scored to 0.000000 on 2026-07-11; all 5
symbols 0.000000 on 2026-07-24).
**Post-gate probs (since the cooldown fix went live 07-22):** shadow rows
store the POST-cooldown-gate probs, so a gate-zeroed side logs 0.000000
while the raw model score is nonzero — that re-scores as a large fake
"divergence". Exclude/expect rows whose INFERENCE line carries the
`cooldown[...]` tag (verified 2026-07-24: NG short "divergence" of 0.58
was exactly the 3 gate-zeroed bars, countdown tags matching).
**Pairing rule:** compare each shadow window against the model that was
deployed DURING that window — after a re-pin, yesterday's rows belong to
yesterday's pickle (this produced false 0.04–0.06 "divergence" for ES/GC on
first attempt). Also check the pickle's mtime: a pkl overwritten AFTER the
child launched means disk ≠ in-memory model and re-scores will show real
residuals until the child restarts.

### 4. Deployed-Threshold / Firing-Rate Sanity (always-on detector)

The Jul-2026 ES incident: deployed long threshold 0.30 sat below the model's
entire probability range (min 0.36) → the "model" fired on 100% of bars =
always-long beta, model unused. This check catches that class.

// turbo
```bash
# Git Bash (needs pandas — global python or trader env)
python - <<'PY'
import json, pandas as pd, sys
fleet = json.load(open('configs/fleet/fleet_manifest.json', encoding='utf-8-sig'))
bad = []
for inst in fleet['instances']:
    if not inst.get('enabled'): continue
    cfg = json.load(open(inst['config'], encoding='utf-8-sig'))
    name = cfg.get('nickname', inst['config'])
    try:
        d = pd.read_csv(cfg['models']['long']['predictions_path'], parse_dates=[0], index_col=0)
    except FileNotFoundError:
        print(f"{name}: predictions_path STALE — fix path, cannot check"); bad.append(name); continue
    for side, col in (('long', 'prob_Buy'), ('short', 'prob_Sell')):
        thr = cfg[side]['tiers'][0]['min_prob']
        p = d[col].dropna()
        rate = (p >= thr).mean()
        rec = p[p.index >= p.index.max() - pd.DateOffset(months=3)]
        rrate = (rec >= thr).mean()
        flag = '' if 0.02 <= rrate <= 0.70 else '  <-- OUT OF BAND'
        if flag: bad.append(f'{name} {side}')
        print(f"{name:32s} {side:5s} thr={thr:.3f} fire(all)={rate:5.1%} fire(3mo)={rrate:5.1%}{flag}")
sys.exit(1 if bad else 0)
PY
```

**PASS**: recent firing rate in ~[2%, 70%] per side. ~100% = always-on (model
unused, threshold below prob mass); ~0% = side effectively disabled (may be
intentional — confirm before waving through).

### 5. Config Parity Report (side-by-side comparison)

Human-readable parameter comparison for each enabled fleet config:

```powershell
$env:PYTHONUTF8 = "1"
conda run -n trader python tests/test_config_parity.py --compare configs/strategies/<CFG>.json
```

All shared parameters should show `OK` in the Match column.

### 6. Verify & Report

| Layer | Tool | What It Catches |
|-------|------|-----------------|
| Roll basis | Step 0 metadata gate | Live features on raw basis (roll seams unadjusted) |
| Config Parity | `test_config_parity.py` | Naming mismatches, missing keys, default divergence |
| Feature Parity | `test_feature_parity.py`, `test_pipeline_parity_hourly.py`, `feature_parity_compare.py` | Training vs live feature computation drift |
| ATR / Exit Parity | `test_per_side_atr.py`, `test_parity_sltp_fill_basis.py`, `test_parity_cooldown_single_authority.py` | Per-side bracket sizing, fill-basis, cooldown bugs |
| Execution Parity | `test_execution_parity.py` | Recovery bars_held time dilation, initial_sl_price schema |
| Prediction Parity | `prediction_parity_compare.py` | End-to-end model inference divergence |
| Threshold Sanity | Step 4 firing check | Always-on / dead-side deployed configs |

Summarize per-layer PASS/FAIL back to the user. Any FAIL is deploy-blocking.
Follow-ons when green: `/validate-ledger-parity` (trade-ledger reconciliation);
after fleet restarts, re-check Step 0 (metadata only activates on restart).
