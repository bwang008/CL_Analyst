# Ticket Resolution Blueprint — sweeper-floor-crash_07052026_0631
**Ticket Directory:** `.agents/collab/tickets/sweeper-floor-crash_07052026_0631/`

## Bug Summary
The GCP post-optimizer pipeline crashes with `FATAL ERROR: unified_pair_optimizer.py produced 0 pairs!` when running symbols with low base rates (e.g., ZC/Corn at ~17%) and reduced feature sets. The root cause is a hardcoded threshold sweep floor of `0.50` in two files. When a model's peak prediction probabilities flatten below `0.50` (common for low-base-rate symbols with fewer features), the optimizer finds zero viable trades across all trials, producing an empty `top_pairs.json` and crashing the VM.

**Crash chain:** `vm_post_optimize.sh` → `batch_post_optimizer.py` → `strategy_optimizer.run_optimization()` → `_PARAM_RANGES["entry_threshold"]` floor of `0.50` → 0 trades → 0 pairs → `FATAL ERROR` → `exit 1`

## Target Files
- `agent/strategy_optimizer.py` (line 699) — **PRIMARY**: This is the file called by the GCP pipeline
- `agent/execution_param_sweeper.py` (lines 40-41) — **SECONDARY**: Legacy/standalone sweeper, not invoked by GCP scripts but should be kept consistent

## Required Changes

### File 1 (PRIMARY): `agent/strategy_optimizer.py`

On **line 699**, in the `_PARAM_RANGES` dictionary, change the `entry_threshold` lower bound from `0.50` to `0.30`:

```diff
-    "entry_threshold":                (0.50,  0.80, 0.03, "float"),
+    "entry_threshold":                (0.30,  0.80, 0.03, "float"),
```

**Rationale:** This allows the Optuna sweeper to search thresholds as low as 0.30, enabling it to capture trades from low-confidence models like ZC. For high-confidence symbols like CL, Optuna's objective function (with `trades < TRADES_PER_YEAR_FLOOR` penalty and `pnl / drawdown` scoring) will naturally self-select thresholds in the higher range. No regression risk for existing symbols.

### File 2 (SECONDARY): `agent/execution_param_sweeper.py`

On **lines 40-41**, in the `objective()` function, lower both `long_threshold` and `short_threshold` floors from `0.50` to `0.30`:

```diff
-    long_thresh = trial.suggest_float("long_threshold", 0.50, 0.65, step=0.01)
-    short_thresh = trial.suggest_float("short_threshold", 0.50, 0.65, step=0.01)
+    long_thresh = trial.suggest_float("long_threshold", 0.30, 0.65, step=0.01)
+    short_thresh = trial.suggest_float("short_threshold", 0.30, 0.65, step=0.01)
```

**Rationale:** Consistency with the primary sweeper. Same safety guarantees apply — the `trades < 200` penalty on line 71 prevents overly-permissive thresholds from being selected.

## Safety Notes
- **CL/ES impact:** None. Optuna's penalty functions constrain threshold selection per-symbol. CL models predict >0.50 naturally, so the optimizer will continue settling on `[0.50, 0.65]` as before.
- **Search space expansion:** `[0.30, 0.80]` with step 0.03 = 18 grid points vs previous 11. ~63% more points, well within typical trial budgets (100-500).
- **No config format changes:** Output JSON configs remain structurally identical.
