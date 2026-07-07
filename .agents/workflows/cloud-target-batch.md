---
name: cloud-target-batch
description: Stage-1 TARGET SCREEN — cheaply rank many candidate targets by out-of-sample edge (fixed-param LGBM per target, NO Optuna) to decide which are worth a full /run-cloud-batch sweep. Produces AUC_Model_Report.md. The precursor step in the two-stage funnel; shortlist the winners into Stage 2 (/run-cloud-batch).
---

# /cloud-target-batch — Stage-1 Target Screen

You are screening candidate prediction **targets** for genuine out-of-sample edge before
spending an Optuna sweep on them. For each target this trains ONE fixed-param LightGBM (no
Optuna, no backtest), and reports holdout ROC-AUC / PR-AUC + a tradeability proxy, ranked.

> **Why (memory `si-01b-edge-and-threshold-floor`):** target choice — not model tuning —
> drove the SI results. 4x1_36H was dead (holdout AUC ~0.50) while 3x1_6H had real edge (0.64),
> and **pos-rate does NOT predict learnability** (4x1_36H had *more* positive labels yet no
> edge). The only reliable selector is the trained-model holdout AUC — this screen computes it
> cheaply so you optimize only targets that clear an AUC gate.

## Two-stage funnel
1. **Stage 1 (this workflow):** screen many targets × datasets → `AUC_Model_Report.md` → shortlist.
2. **Stage 2 ([/run-cloud-batch](run-cloud-batch.md)):** full E2E + Optuna ensemble optimization
   on the FEW shortlisted targets (with the distribution-relative threshold floor,
   `strategy_optimizer.py` firing band `[0.05, 0.45]`).

## How to run (local / single VM — available now)
The screen is a mode of the E2E pipeline. It reads a MasterConfig JSON whose
`training_workflow.mode = "screen"` and `training_workflow.target_columns` is the list to screen.

1. Generate a **validated** screen config with `scripts/build_screen_config.py` (mode is
   already `screen`; `execution_workflow` is NOT required in screen mode). Use EITHER a
   dataset (screen the full target grid it contains) OR a v2 batch manifest (screen exactly
   that batch's targets):
   ```bash
   # from a dataset (screen every TARGET_TRIPLE_*_LONG/_SHORT in the parquet)
   conda run -n trader python scripts/build_screen_config.py \
     --from-dataset HourSet_14B --symbol CL --train-cutoff-date 2025-06-01
   # or from a v2 batch manifest (deduped union of every experiment's target_columns;
   # symbol / dataset_version / train_cutoff_date are read from the manifest baseline)
   conda run -n trader python scripts/build_screen_config.py \
     --from-manifest configs/batch_manifest_v2_hourset14b_scout.json
   ```
   - Writes `configs/sweeps/screen_<symbol>_<dataset>.json` by default (override with `--out`).
   - `train_cutoff_date` = train on data before it; the screen scores AUC on the vault (>= cutoff).
     Leave `holdout_cutoff_date` unset for the default 2-way split (`--holdout-cutoff-date` to override).
   - The generator VALIDATES the config via `MasterConfig(**cfg)` and fails loud (non-zero,
     writes nothing) on an invalid config or zero targets — no half-written config.
   - Shape matches the reference template
     [configs/sweeps/screen_si_hourset01b.json](../../configs/sweeps/screen_si_hourset01b.json)
     (hand-editing it still works if you prefer).
2. Dry-run (validates config + resolves the dataset, no training):
   ```bash
   conda run -n trader python gcp/vm_e2e_pipeline.py \
     --master-config configs/sweeps/screen_si_hourset01b.json --mode screen \
     --output-dir reports/screens/si_01b --dry-run
   ```
3. Run the screen (~13s/target on the full SI history):
   ```bash
   conda run -n trader python gcp/vm_e2e_pipeline.py \
     --master-config configs/sweeps/screen_si_hourset01b.json --mode screen \
     --output-dir reports/screens/si_01b
   ```
   `--mode screen` on the CLI overrides the manifest; omit it to use `training_workflow.mode`.
   Reproducible under a fixed `--random-seed` (default 42).

## Output — `reports/screens/<...>/AUC_Model_Report.md`
One row per (symbol, target, direction), **sorted by holdout ROC-AUC desc**:

| column | meaning |
|---|---|
| AUC train / **AUC holdout** | ROC-AUC vs the target's own label. **Holdout is the gate.** |
| PR-AUC holdout | precision-recall AUC (rare-target sanity) |
| pos% train / holdout | base rate of positive labels (NOT a proxy for edge) |
| prob spread | q95−q05 of holdout probs (compressed ⇒ hard to threshold selectively) |
| signals/yr | est. trades/yr at the q80 reference threshold (the "trades" axis) |
| precision@ref | positive rate among the top-20% highest-prob bars (edge concentration) |

## Interpreting it (shortlist rule)
- **holdout AUC ≥ ~0.55 = real edge** → shortlist for Stage 2. **≈ 0.50 = no edge** → discard,
  no matter the pos-rate or train AUC (train AUC ~0.95+ on all targets is just train overfit).
- Prefer targets that are BOTH high-AUC AND have enough `signals/yr` (a compressed
  `prob spread` with few signals can't be made selective — flag it).
- Validated on real SI data: the screen reproduced the manual investigation exactly —
  3x1_6H_LONG 0.66 / 2x1_3H_SHORT 0.60 (keep) vs 4x1_36H_LONG 0.52 / 4x1_36H_SHORT 0.50 (drop).

## Scale-out (cloud fan-out) — status
Running the screen across many symbols × datasets in parallel on GCP is the intended scale
path (a `mode: screen` branch of `gcp/run_sweep_batch.ps1` that skips the Optuna sweep and the
post-optimizer, fanning one VM per symbol×dataset and collecting each `AUC_Model_Report.md`).
That orchestration is specced in `.agents/collab/tickets/` but **not yet wired** — it edits
critical PowerShell/bash that must be validated with a real (billable) GCP dry-run, which is a
human-launched step. Until then, run the screen via the CLI above (one config per symbol);
it's fast enough (seconds/target) to sweep a full target grid locally or on one VM.

## Related
- Fix that this feeds: distribution-relative threshold floor — `agent/strategy_optimizer.py`
  `entry_threshold` firing band (tickets `dynamic-entry-threshold_*`, `firing-band-manifest_*`).
- Forensic audit of a completed batch: [/model-detective](model-detective.md).
