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
One row per (symbol, target, direction), **sorted by holdout ROC-AUC desc** (raw PR-AUC is
base-rate-dominated and NOT comparable across targets, so it's a column, not the sort key):

| column | meaning |
|---|---|
| PR-AUC / **ROC-AUC** | holdout area under the PR / ROC curve. **ROC-AUC is the sort key and the gate.** |
| PR-lift | PR-AUC / base rate (>1 beats a random classifier at that base rate) |
| Brier | calibration (lower better) of the fixed-param, focal-trained SCREEN model — rough/relative signal, NOT the tuned Stage-2 model's calibration |
| pos% / n_pos | holdout positive rate / count. `n_pos < 75` ⇒ `RARE` flag (AUC unreliable on that little support) |
| prec@ref | precision at the top-20% firing threshold on holdout (edge concentration) |
| RR | reward:risk (TP/SL) parsed from the target name |
| EV_flr | **pessimistic** expected R per signal at the top-20% threshold, assuming every non-win is a full -1R stop (ignores timeouts/partial exits). A trap-detector, NOT a profitability estimate — true EV comes from the Stage-2 backtest |
| $win | gross $ of a full TP winner = `tp_mult × median holdout ATR × $/pt` |
| cost% | round-trip slippage + est. $4 commission as a % of `$win` (uses the symbol's default slippage/side). **Approximate** — the Stage-2 backtest is authoritative |
| flag | `RARE` / `cost?` / `KEEP` / `~tune` / `drop` — see shortlist rule below |

## Interpreting it (shortlist rule)
The `flag` column encodes this directly, in order:
1. **`RARE`** if `n_pos < 75` — support too thin, AUC on hyper-rare targets is unreliable.
2. **`cost?`** if `ROC-AUC ≥ 0.53` AND `cost% > 6%` — ROC says edge, but it's likely a **cost
   mirage**: round-trip slippage+commission eats too much of a TP winner to be tradeable even
   though the model discriminates fine. ZC/ZS grains were the case that motivated this column
   (high AUC, ~0 edge after costs — see the model-detective audit that root-caused it); the
   `1x0p5_1H` short-horizon target repeats the pattern on CL/NG/SI (cost 6.9-7.8%) while staying
   clean on the higher-$/pt ES/GC (3.9-4.9%) — **don't assume a target is safe on one symbol
   just because it screened clean on another; the cost% column is symbol-specific.**
3. **`KEEP`** if `ROC-AUC ≥ 0.55` — real edge, shortlist for Stage 2.
4. **`~tune`** if `ROC-AUC ≥ 0.53` — borderline, may clear the gate after Optuna; worth a slot
   if you have spare experiment capacity, skip otherwise.
5. **`drop`** — `ROC-AUC ≈ 0.50`, no edge, discard no matter the pos-rate (train AUC ~0.95+ on
   every target is just train overfit and is not reported here for that reason).
- Validated on real SI data: the screen reproduced the manual investigation exactly —
  3x1_6H_LONG 0.66 / 2x1_3H_SHORT 0.60 (keep) vs 4x1_36H_LONG 0.52 / 4x1_36H_SHORT 0.50 (drop).
- Validated again on the CL/15B + ES/GC/NG/SI short-horizon standup (`2x1_1H`, `2x1_2H`,
  `1x0p5_1H`): `2x1_1H` came out #1 on 4 of 5 symbols (ROC 0.81-0.85), and the `cost?` flag
  correctly separated the one symbol/target combos where that edge wasn't tradeable — see
  `reports/screens/SUMMARY_SHORT_HORIZON.md` for the full worked example.

## Building the Stage-2 manifest (family-bundling convention)
The shortlist rule above operates per (target, direction) row, but Stage-2 scout manifests
bundle LONG+SHORT of the same barrier definition into one experiment slot (e.g.
`TARGET_TRIPLE_2x1_3H_LONG` + `_SHORT` together) — a "family". To go from a screen report to a
manifest:
1. **Group by family** — strip the `_LONG`/`_SHORT` suffix; a family's score is the best
   ROC-AUC of its two sides.
2. **Rank families** by that score, descending.
3. **Select the top N** (4-6 is typical), skipping any family whose best side is flagged
   `cost?` or `RARE` — prefer the next-clean family instead of shipping a mirage into an
   Optuna sweep (Stage 2 will just tune the mirage harder, not make it disappear).
4. **Build the manifest** by copying an existing sibling scout manifest for that symbol as a
   template (e.g. `configs/batch_manifest_v2_si_hourset01b_scout.json`) — keep
   `infrastructure`, `execution_workflow`, and the `optuna` box byte-identical; only change
   `baseline.data_workflow.dataset_version` and replace `experiments` with the N selected
   family slots (`target_columns: [<fam>_LONG, <fam>_SHORT]`). Validate with
   `BatchSweepConfig(**cfg)` before writing — fail loud, write nothing on a validation error.
   CL manifests omit the symbol prefix (`batch_manifest_v2_hourset15b_scout.json`, not
   `batch_manifest_v2_cl_hourset15b_scout.json`) — match whatever convention the symbol's
   existing manifests already use.
5. If the symbol's `train_cutoff_date` used for the *screen* differs from the fleet's
   production training convention (e.g. a shorter window chosen for a faster screen run),
   the **manifest** should still use the production convention (`2022-01-01` across the
   current fleet) — the screen's cutoff is a screening-speed choice, not a training decision.

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
- Worked example of the full loop (new targets → screen → cost-flag triage → top-6 manifest,
  across 5 symbols): `reports/screens/SUMMARY_SHORT_HORIZON.md` and the sibling
  `configs/batch_manifest_v2_{hourset15b,es_hourset02b,gc_hourset02b,ng_hourset02b,si_hourset02b}_scout.json`.
