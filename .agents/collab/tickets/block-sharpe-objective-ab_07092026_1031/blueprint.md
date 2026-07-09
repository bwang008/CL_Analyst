# Ticket Resolution Blueprint — block-sharpe-objective-ab_07092026_1031
**Ticket Directory:** `.agents/collab/tickets/block-sharpe-objective-ab_07092026_1031/`

## Change Summary
Add a **block-wise Sharpe objective** to the strategy post-optimizer (partition the in-sample
monthly PnL series into contiguous calendar blocks, aggregate per-block Sharpes), widen the
post-optimizer holdout from **6 → 12 months**, and run a **4-arm metric A/B sweep**
(`sharpe` baseline vs `block_min` / `block_median` / `block_mean_std`) as separate Optuna
studies inside ONE batch, compared on holdout PnL. Targeted change at the objective-computation
point + orchestration threading; no re-architecture.

## Holdout math (verified against the real dataset — the sizing decision)
`data/processed/CL_HourSet_15B.parquet`: **2010-10-13 → 2026-06-12**. With
`train_cutoff_date = 2022-01-01` and 2-way split (`holdout_cutoff_date: null`), the
post-optimizer predictions window is **2022-01-01 → 2026-06-12 ≈ 53.4 months**.

| `post_optimizer_holdout_months` | In-sample window | Monthly bins | 3-block layout |
|---|---|---|---|
| 6 (current) | 2022-01 → 2025-12 ≈ 47.4 mo | ~48 | 16 / 16 / 16 |
| **12 (chosen)** | **2022-01 → 2025-06 ≈ 41.4 mo** | **~42** | **14 / 14 / 14** |
| 15 | 2022-01 → 2025-03 ≈ 38.4 mo | ~39 | 13 / 13 / 13 |
| 18 | 2022-01 → 2024-12 ≈ 35.4 mo | ~36 | 12 / 12 / 12 |

**Decision: `post_optimizer_holdout_months: 12`, `n_blocks: 3`, `min_block_months: 10`.**
Doubles the holdout (12 monthly observations instead of 6) while keeping every block at ~14
months — inside the requested 12–14-month band with slack over the `min_block_months=10` guard
(41.4 ≥ 3×10 = 30). 18 months would push blocks below 12; 12 is the sweet spot, and blocks only
grow as the data end advances. The trade floor auto-scales (it is computed from the sliced
predictions span at `agent/strategy_optimizer.py:1488-1490`), so no floor retuning is needed.

## Design decisions (deviations from the draft spec, each deliberate)
1. **Block boundaries are a fixed calendar partition of the sliced in-sample predictions
   window** (post-holdout-carve `predictions_df.index.min()/max()`), identical for every trial —
   NOT a partition of each trial's observed monthly-PnL span. Rationale: (a) Optuna scores must
   be comparable trial-to-trial; (b) a config that only trades 2024-2025 must NOT get its three
   blocks squeezed into its active period — `block_min` is supposed to punish exactly that.
   Within the block scorer, reindex the monthly series to the full calendar month range with
   0-fill so inactive months count. Do NOT apply this reindex to the baseline `sharpe` path
   (it must stay byte-identical).
2. **Empty/degenerate block ⇒ block Sharpe = 0.0** (zero trades, or block std < 1e-9). Not
   −9999: median/mean_std stay well-defined, and `block_min ≤ 0` already rejects the trial
   without a nuke. Matches the spec's std=0 rule; extends it to the no-trades case.
3. **Cap per-block AND after aggregation.** Each block Sharpe is clipped to
   ±`OBJECTIVE_SCORE_CAP` (5.0) before aggregating, then the aggregate is capped, then the
   trade-floor sigmoid applies last (unchanged). Spec said cap only after aggregation; the
   per-block clip is added because a single lucky 14-month block can produce Sharpe 30 and drag
   `block_mean_std`'s mean — the same low-downside pathology the cap exists for. `block_min`/
   `block_median` are unaffected by the extra clip.
4. **Guard violation = hard fail, never fallback.** If in-sample months <
   `n_blocks × min_block_months` for a block metric: preflight fails the dry run, and
   `run_optimization` raises at runtime. The spec's "fall back to single_sharpe" is rejected —
   house rule: no silent fallbacks/defaults.
5. **Baseline arm keeps the name `sharpe`** (spec's `single_sharpe` is rejected). Artifact names
   (`batch_summary_optimized_sharpe.md`…), `compare_parity.py`'s REQUIRED set, seed offset 0,
   and byte-equivalence with historical runs all hang off the existing name. New metric values:
   `block_min`, `block_median`, `block_mean_std`.
6. **One sweep, four post-opt arms, one batch folder** (spec's "full pipeline once per metric"
   and per-metric folders are rejected). Model training is objective-independent; running the
   sweep once means all arms score literally identical models/predictions — a cleaner A/B and
   ~4× cheaper. The multi-objective machinery already exists (`--objective both` runs
   sharpe+sortino concurrently in one pool with per-objective reports,
   `agent/batch_post_optimizer.py:848-996`) — extend it to N arms. Per-arm identity lives in
   artifact suffixes, exactly like the sharpe/sortino era.
7. **Objective + block params stay deploy-chain CLI parameters, not manifest fields**
   (precedent: ticket `drop-sortino-objective_07042026_2301` deliberately kept the objective out
   of `BatchSweepConfig` so old manifests run unchanged). The holdout is already a manifest field
   and changes there. All block params are echoed into report headers (self-describing runs).
8. **Sortino is not an A/B arm** (it already lost its A/B on every symbol); code paths stay
   intact and `both` remains a valid alias.

## Isolation requirement (the one real chain bug to avoid)
`agent/unified_pair_optimizer.py:127-152` currently POOLS objectives: it reads the sharpe AND
sortino summary mds, dedupes across them by robustness, and emits a single `top_pairs.json`.
Run as-is with 4 arms, pass-2 pair selection would cross-contaminate the arms. Pass-1 → pair
selection → pass-2 must be **per-arm end-to-end**: each arm selects its own top pairs from its
own pass-1 report and re-optimizes them under its own objective.

Known accepted caveat (pre-existing, do not fix here): the pair-selection robustness score
(`pnl_opt + 6×pnl_holdout`) peeks at per-side holdout PnL. It applies identically to every arm,
so the cross-arm comparison stays fair; the truly unseen verdict is the pass-2 ensemble holdout,
which is what the A/B reads.

## Target Files

### 1. `agent/strategy_optimizer.py` — the objective point
- `_OBJECTIVE_SEED_OFFSETS` (line 74): add `{"block_min": 2, "block_median": 3,
  "block_mean_std": 4}` so each arm's study seeds deterministically and differently
  (`effective_seed` at line 1326).
- New module constant `BLOCK_OBJECTIVE_METRICS = {"block_min", "block_median", "block_mean_std"}`
  and helper:
  ```
  _block_sharpe_score(monthly_pnls: pd.Series, window_start, window_end,
                      n_blocks, metric, lambda_dispersion) -> float
  ```
  Algorithm: reindex monthly series to the full calendar month range of
  [window_start, window_end] with fill 0 → partition into `n_blocks` contiguous near-equal
  blocks in time order (remainder months to the EARLIEST blocks) → per block: annualized
  monthly Sharpe `mean/std*sqrt(12)`, 0.0 if no trades or std < 1e-9, clip to ±5.0 → aggregate:
  `block_min` = min; `block_median` = median; `block_mean_std` = mean − λ·std(per-block Sharpes).
  No harmonic/geometric means anywhere (signed values).
- Objective scoring (lines 1228-1261): keep the monthly-series construction as-is; dispatch on
  `objective_metric` — `sharpe`/`sortino` branches byte-identical to today; block metrics call
  the helper, then `min(agg, OBJECTIVE_SCORE_CAP)` then `_apply_trade_floor_penalty` exactly as
  the existing branches do.
- `_compute_objective_score` (lines 973-1021, the regression-guard mirror): same dispatch; gains
  the window-bounds + block params so baseline and optimized scores stay on one scale.
- `make_objective`: accept `n_blocks`, `lambda_dispersion`, and the in-sample window bounds
  (computed once from the already-sliced `predictions_df` — the holdout is carved upstream at
  lines 1397-1405 and never recomputed, per the existing comment at 796-797).
- `run_optimization` (line 1290) / `run_ensemble_optimization` (line 1713): thread the block
  params; when `objective_metric ∈ BLOCK_OBJECTIVE_METRICS`, hard-raise if in-sample months <
  `n_blocks × min_block_months` (loud, with the computed layout in the message).
- **Per-block diagnostics (all arms, including baseline `sharpe`):** for the best config, store
  `optuna_info.block_sharpes = [s1, s2, s3]` (+ block boundary dates) computed with the same
  helper regardless of which metric drove selection. Costs one extra aggregation on the winning
  trial; makes every arm's report show WHERE the score comes from (which block binds under
  `block_min`, dispersion under `block_mean_std`) and lets block-min be applied post-hoc as a
  FILTER on the sharpe arm's leaderboard (tracker top-K) without a separate study.
- vbt warm-start prescreener (`run_vbt_prescreener`, lines 495-655): UNCHANGED — for block
  metrics pass `"sharpe"` as its scoring metric. Seeds are starting points, not selection; the
  study itself scores block-wise.
- CLI `--objective` choices (line 2188): extend with the three block metrics.

### 2. `agent/batch_post_optimizer.py` — N-arm fan-out
- `--objective` (line 1046): accept a comma-separated list; validate every element ∈
  {sharpe, sortino, block_min, block_median, block_mean_std}; keep `both` → `sharpe,sortino`.
- New args `--n-blocks` (default 3), `--lambda-dispersion` (default 1.0), `--min-block-months`
  (default 10); threaded through `run_single_optimization` (lines 349-382) into
  `run_optimization`/`run_ensemble_optimization`.
- `objectives` list (line 1287) already drives the concurrent pool, per-objective result buckets,
  and per-objective reports/JSONs (`batch_summary_optimized_{obj}.md`,
  `optimization_results_{obj}.json` via `_finalize_objective_results`, lines 782-846) — the
  4-arm fan-out is nearly free.
- Report header (~line 452): add `objective_metric`, `n_blocks`, `lambda_dispersion`,
  `min_block_months`, `holdout_months` lines. Keep all existing columns (Trades pre/opt/ho
  T/L/S, PF pre/opt, PnL pre/opt/holdout, Opt Thr, Best Trial) — PnL (holdout) is the
  comparison column. Add a per-row `Block Sharpes` column (e.g. `1.8/0.4/2.1`) from
  `optuna_info.block_sharpes` so every arm's report is interpretable at a glance.

### 3. `agent/unified_pair_optimizer.py` — per-arm selection
- Replace the hardcoded sharpe+sortino md pair (lines 127-133) with `--objectives` (default
  `sharpe`, preserving today's effective behavior on sharpe-only runs).
- Single objective given → read only that arm's md, write `top_pairs.json` when the arm is
  `sharpe` (parity-compatible), else `top_pairs_<arm>.json`. NO cross-arm dedup ever.

### 4. `gcp/vm_post_optimize.sh` — per-arm chain
- `OBJECTIVE` (line 175): accept comma list; validate elements; pass through to pass-1
  `batch_post_optimizer` (which runs all arms concurrently in one pool).
- Steps [4b]/[4c] (lines 529-563): loop per arm — `unified_pair_optimizer.py --objectives <arm>`
  → arm's top-pairs JSON → pass-2 `batch_post_optimizer.py --objective <arm>
  --target-pairs-json <arm's json>`. Pass 2 is light (~4 pairs, warm VM).
- Thread `--n-blocks/--lambda-dispersion/--min-block-months`.
- `generate_ensemble_artifacts.py` call: pass the arm list (its objectives param + skip logic
  already exist; produces `<arm>_ensemble_backtests.md`).
- Verify the artifact-upload globs (line ~600 "glob for all objectives") match
  `batch_summary_optimized_block_*.md`, `optimization_results_block_*.json`,
  `block_*_ensemble_backtests.md`, `top_pairs_*.json`.

### 5. `gcp/gcp_deploy_optimizer.ps1` — deploy threading + TTL
- `-Objective` (line 33): allow comma list, forwarded verbatim (launch cmd line 305). Add
  `-NBlocks/-LambdaDispersion/-MinBlockMonths` pass-through flags.
- Parameterize `--max-run-duration` (line 153, currently hardcoded 360m). 4-arm scout ≈
  6 experiments × 2 metrics × 2 sides × 4 arms = **96 pass-1 studies** @200 trials on
  n2-standard-32 → ~3 pool waves ≈ 3× a sharpe-only post-opt. Use **720m** for the 4-arm run,
  keeping TTL > monitor timeout (orphan-prevention rule: raise both together).

### 6. `gcp/run_sweep_batch.ps1` — orchestrator
- New `-Objective` parameter (default `"sharpe"` — today's behavior), forwarded to
  `gcp_deploy_optimizer.ps1` along with the block params; scale the optimizer task-count math
  by the number of arms (the drop-sortino ticket set it to `completed * 4`; generalize to
  `completed * 4 * n_arms`).
- **Auto-stamp the batch folder** (replaces the manual rename step in the workflow doc):
  `batch_<timestamp>_<SYMBOL>_<TIER>[_OBJAB]` — symbol from manifest `baseline.symbol`, tier
  regex from the manifest filename (canary|scout|prod), `_OBJAB` suffix when >1 arm.

### 7. `scripts/preflight_holdout_check.py` — block-size gate
- Accept the objective list + block params (forwarded by `run_sweep_batch.ps1` at dry-run); when
  any block metric is present: FAIL unless
  `(window_months − holdout_months) ≥ n_blocks × min_block_months`, and print the computed block
  layout (calendar boundaries + months per block) so the dry run documents the split.

### 8. Manifests
- `configs/batch_manifest_v2_hourset15b_scout.json` (and any manifest used for this A/B):
  `post_optimizer_holdout_months` 6 → **12**. Leave other manifests untouched until their next
  run is planned.

### 9. NEW `scripts/compare_objective_arms.py` — the A/B readout
- Reads the per-arm `batch_summary_optimized_{arm}.md` + `optimization_results_ensembles_{arm}.json`
  from one batch dir; emits `objective_ab_summary.md`: rows = experiment/ensemble, columns = per
  arm {PnL(opt), PnL(holdout), Trades(holdout)}, holdout PnL leading. Header repeats each arm's
  block params. Expected pattern (documented in the report itself): block arms' OPT PnL will be
  lower than baseline's by construction — the verdict is holdout PnL only.

### 10. Docs — `.agents/workflows/run-cloud-batch.md`
- Objective section: the four metrics, deploy-chain invocation
  (`-Objective "sharpe,block_min,block_median,block_mean_std"`), block params + defaults.
- Date-controls section: holdout 6→12 rationale (12 holdout observations; 3×14-month blocks on
  the 2022-01→2026-06 CL window) and the new preflight block gate.
- Replace the manual folder-rename convention with the auto-stamp.
- TTL note for multi-arm runs (720m).

## Explicit Non-Changes (guard rails)
- Trade-floor sigmoid, `OBJECTIVE_SCORE_CAP`, firing-frac floor, triple-barrier labels/exits,
  dry-run gates, TTL orphan defenses, config-validation gate: mechanics untouched (block scorer
  slots in upstream of cap+floor exactly where raw Sharpe sits today).
- `sharpe`/`sortino` scoring paths byte-identical (validation gate 2 enforces it).
- `scripts/compare_parity.py`: unchanged; parity targets remain sharpe-only canaries. Verify
  additive block artifacts don't trip it (its checks are REQUIRED-list + per-file content loops,
  so extra files should be inert).
- `src/config/schemas.py` / `scripts/generate_v2_manifest.py`: no new manifest fields.
- The vectorbt prescreener scoring.
- `unified_pair_optimizer`'s robustness formula (documented leak caveat above).

## Tests
- `tests/test_objective_seed_offset.py`: extend for the three new offsets (2/3/4) + divergence.
- NEW `tests/test_block_sharpe_objective.py`:
  - partition: near-equal contiguous blocks, remainder to earliest (e.g. 42→14/14/14, 41→14/14/13);
  - calendar 0-fill reindex (inactive months count; boundaries from window, not trade span);
  - empty block → 0.0; std<1e-9 → 0.0; per-block clip at ±5.0;
  - aggregation math for min/median/mean_std incl. negative block Sharpes and λ;
  - guard: hard-raise when months < n_blocks×min_block_months;
  - **sharpe regression**: on a synthetic trade set, the refactored `sharpe` path returns the
    exact pre-change score.
- Arg-parsing tests: comma-list `--objective` validation (reject unknown metric), `both` alias.

## Validation Gates (merge blocked until all pass)
1. Full local suite green (`conda run -n trader python -m pytest`).
2. **Baseline invariance:** 14B canary sharpe-only at fixed seed before/after this change →
   byte-comparable sharpe artifacts (pipeline determinism already proven; seed offset 0
   unchanged). `compare_parity.py` exits 0.
3. **A/B canary:** canary manifest, `-Objective "sharpe,block_min"`, holdout 12 → both artifact
   sets present, per-arm top-pairs files, block_min pass-2 pairs traceable ONLY to block_min
   pass-1 rows (no cross-arm contamination), self-describing headers, VM inside TTL.
4. Preflight gate: a window that can't fit `3×10 + 12` months fails the dry run with the block
   layout printed.
5. Real run: 15B scout, all four arms, `objective_ab_summary.md` produced; verdict read on
   ensemble holdout PnL per arm.

## Rollback
- Operational (zero code): `-Objective sharpe` = today's pipeline exactly; revert manifest
  holdout 12→6 if desired.
- Full: single-commit revert; gate 2 guarantees the baseline never drifted.
