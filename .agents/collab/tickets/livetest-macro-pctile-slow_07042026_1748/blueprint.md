# Ticket Resolution Blueprint — livetest-macro-pctile-slow_07042026_1748
**Ticket Directory:** `.agents/collab/tickets/livetest-macro-pctile-slow_07042026_1748/`

## Bug Summary
Live/livetest per-bar feature generation is pathologically slow (10.58 s/bar measured in the pinned `trader` env), making parity livetests take ~10x longer than the documented ~1 bar/sec. Root cause: `MacroFeatureEngine._build_fred_features` (`src/features/macro_features.py:484-489`) computes the 12 `MACRO_*_PCTILE_*D` features via `series.rolling(w).apply(lambda x: pd.Series(x).rank(pct=True).iloc[-1], raw=False)` — a Python-level lambda invoked once per window position over the entire 13,138-row daily FRED history (1954→2026), re-executed on EVERY bar via `build_live_features → merge_all → _build_fred_features`. py-spy profile of a live run: 96.6% of all CPU time in this loop. The identical anti-pattern exists in `_build_cot_features` (5 COT percentile blocks, minor cost). Not a recent regression — dates to commit f9122b28 (2026-03-22), untouched since.

Approved fix: replace the lambda with pandas' native Cython `Rolling.rank(pct=True)` (available since pandas 1.4.0; pinned trader env is 1.5.3). Proven bitwise identical by BOTH Auditor and Impact-Reviewer via independent equivalence harnesses on the real `fred_macro_data_cl.csv` / `cftc_cot_cl.csv` (batch path + live-override path + adversarial tie/NaN synthetics: max_abs_delta=0.0, identical NaN masks). Measured speedup ~339-360x; restores ~1 bar/sec livetest throughput. Backtest/live parity preserved by construction (batch and live share this function; outputs bit-identical).

## Target Files
- `src/features/macro_features.py`
- `requirements-dev.txt`
- `tests/` (new unit test for rank-equivalence semantics)

## Required Changes
1. **`src/features/macro_features.py` — `_build_fred_features`, lines 485-489:** Replace the `rolling(w).apply(lambda ...)` percentile computation with the native rolling rank: for each `w` in `PCTILE_WINDOWS`, assign `features[f"MACRO_{col}_PCTILE_{w}D"] = series.rolling(w).rank(pct=True)`. No other behavior (column names, window list, loop structure) may change.
2. **`src/features/macro_features.py` — `_build_cot_features`:** Apply the same mechanical substitution to all 5 COT percentile blocks (lines 553-556 MM_Net 52W, 559-562 MM_Net 14/35W, 569-572 Prod_Net 52W, 577-580 Spec_Net 52W): replace each `rolling(w).apply(lambda x: pd.Series(x).rank(pct=True).iloc[-1], raw=False)` with `.rolling(w).rank(pct=True)` on the same series/window. Output column names unchanged. (Confirmed report-type-agnostic: TFF vs legacy COT normalization happens upstream in `scripts/download_macro_data.py`; this applies uniformly to all 8 symbols.)
3. **`requirements-dev.txt` line 14 (REQUIRED, per Reviewer condition):** Bump the pandas floor from `>=1.3.0` to `>=1.4.0` — `Rolling.rank` does not exist before 1.4 and a 1.3 env would raise `AttributeError`. All real envs already satisfy it (trader=1.5.3, gcp>=1.5.0, dashboard>=2.0).
4. **New unit test (REQUIRED, per Reviewer condition):** Pin the equivalence `series.rolling(w).rank(pct=True)` ≡ `series.rolling(w).apply(lambda x: pd.Series(x).rank(pct=True).iloc[-1], raw=False)` on a fixture containing heavy ties, leading NaNs, mid-series NaN, and an all-equal window. This locks tie/NaN semantics against future pandas upgrades.
5. **Pre-merge validation (REQUIRED, per Reviewer condition):** Re-run the 336-bar parity livetest (`reports/_ledger_parity/` setup) and confirm trade-ledger identity vs the backtest per `.agents/workflows/livetest.md` Step 5. Expected wall clock drops from ~60+ min to single-digit minutes.
6. **Documentation (recommended):** Update the Performance Expectations table / rule-of-thumb in `.agents/workflows/livetest.md` after measuring the new throughput, since "~1 bar/sec" reflected the pre-fix cost profile.

## Approvals
- Ticket-Auditor: root cause + fix proposed, severity MEDIUM, not a recent regression (no fast track).
- Ticket-Impact-Reviewer: APPROVED (no human authorization required; Interface Rule not triggered, single-component change). Conditions 1-3 above folded into Required Changes items 3, 4, 5.
