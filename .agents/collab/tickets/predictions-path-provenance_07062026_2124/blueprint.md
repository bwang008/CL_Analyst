# Ticket Resolution Blueprint — predictions-path-provenance_07062026_2124
**Ticket Directory:** `.agents/collab/tickets/predictions-path-provenance_07062026_2124/`

## Bug Summary
4 of 5 live fleet configs point `models.*.predictions_path` at a file that does
not exist on disk:

| Config | predictions_path |
|---|---|
| HS14B_Sharpe_E01 (CL flagship) | ❌ missing |
| ES01B_Sortino_E01 | ❌ missing |
| NG01B_Sharpe_E03 | ❌ missing |
| GC01B_Sharpe_E04 | ✅ correct (promoted from `PRODUCTION_…_GC_01B_SCOUT_PASS`) |
| SI01B_Sharpe_E02 | ❌ missing |

**Live impact: ZERO.** `predictions_path` is never read anywhere under
`src/live_execution/` (grep-verified). Live inference loads `model_path` and
computes features from live bars; the predictions CSV is a backtest/OOS artifact.
This is why all four have traded fine despite the broken path.

**Root cause:** the config promotion/generation step writes `predictions_path` as a
**bare** `reports/batch_runs/batch_<timestamp>/predictions/...`, but the actual
downloaded batch dir carries a suffix (`_<SYM>_01B_SCOUT`, `PRODUCTION_…_PASS`).
The `model_path` survives because it resolves against the existing `sweep_*` dir.
GC is correct only because it was promoted from a properly-named PASS dir.

**Where it *does* bite (offline only):** the strict CONFIG VALIDATION GATE
(build-symbol-pipeline Phase 6 check e), any backtest/parity/report that re-reads
OOS predictions, and provenance (tracing a live config to the OOS run that
justified it).

## Target Files
- `agent/generate_ensemble_artifacts.py` — config stamping (writes predictions_path).
- `agent/batch_post_optimizer.py` — target-pairs promotion path (also writes configs).
- `agent/strategy_optimizer.py` — if it emits predictions_path in `_opt_`/`_hybrid_` configs.
- Backfill (data, not code): `configs/strategies/HS14B_Sharpe_E01_06262026.json`,
  `ES01B_Sortino_E01_07062026.json`, `NG01B_Sharpe_E03_07052026.json`,
  `SI01B_Sharpe_E02_07062026.json`.
- Docs/schema: `src/config/schemas.py` (or wherever the model block is validated),
  `configs/strategies/config_readme.md`.

## Required Changes
1. **Fix the generator** so `predictions_path` is stamped from the SAME resolved
   batch-dir root used for `model_path` (i.e., the actual downloaded dir name with
   its suffix), not a reconstructed bare `batch_<timestamp>`. A generated config
   must never carry a predictions_path that does not exist at generation time.
2. **One-time backfill** of the 4 stale live configs to the real CSVs on disk
   (e.g. SI → `reports/batch_runs/batch_20260706_0908_SI_01B_CANARY/predictions/SI_Sharpe_E02_predictions.csv`,
   or the SCOUT dir if it holds one). Verify each corrected path exists after edit.
3. **Document the contract:** `predictions_path` is a provenance/backtest artifact
   NOT consumed by live execution — a broken path is not a live hazard but IS a
   config-validation-gate failure and a provenance break. Confirm with user whether
   to keep it required-and-correct (recommended for traceability) or make it
   explicitly optional in the schema. Default recommendation: keep required + correct.
4. **Regression test:** a generated/promoted config's `predictions_path` must exist
   on disk (extend the config-validation coverage so this can't silently regress).

## Severity
LOW — no live impact; long-standing (not a fresh regression). Fast-trackable per
/ticket-manager Step 2.

## Dependencies / Coordination
- Backfilling ES01B_Sortino_E01's predictions_path overlaps
  `es-config-drift-repin_07062026_2124` (both touch ES + `test_config_generator_symbols.py`).
  Coordinate the ES predictions assertion between the two.
