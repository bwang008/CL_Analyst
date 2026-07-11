# Bug Report — jit-roll-ratio-empty_07102026_1453

**Severity (reporter's assessment):** HIGH — live fleet inference runs on the wrong price basis; measurably flipped NG entry signals during the 2026-07-06..10 losing week. NOT a recent regression: the defect has existed since the per-symbol 1h seeds were staged (2026-07-04/05 era).

## Symptom

Live fleet models (CL/ES/NG/GC/SI, all `bar_size: "1h"`) underperform their backtests. Investigation on 2026-07-10 found live feature generation consumes the RAW stitched futures series while every deployed model was trained on the RATIO-ADJUSTED continuous series (HourSet datasets).

## Root cause (two components, same subsystem)

### Component 1 — the JIT ratio ledger was never populated for seed-era rolls (the active poison)

- Per-bar inference calls `DataManager.get_ratio_adjusted_df()` (src/live_execution/live_trader.py:4325) before `build_live_features()`. The intended "split-brain" design: features on ratio-adjusted basis, execution/bracket-ATR on raw basis.
- `get_ratio_adjusted_df()` (src/live_execution/data_manager.py:1092-1114) does NOT compute an adjustment; it replays recorded ratios from `self._roll_ratios`, restored at startup from `roll_history` in `.roll_metadata_<SYM>.json` (data_manager.py:357-367).
- **All five metadata files under `C:\CL_Analyst_Data\data\processed\` have `"roll_history": []` and `"cumulative_ratio": 1.0`.** With an empty list the function early-returns the raw cache verbatim → inference basis == raw.
- The 1h seeds (`<SYM>_raw_1h.parquet`) were staged from the raw execution series and contain ~9 months of unadjusted roll seams. The JIT mechanism only records a ratio when it witnesses a roll at startup (`_detect_rollover` + `_compute_roll_ratio`); it has no mechanism to backfill ratios for rolls already inside the seed history. First run per symbol → "no previous front-month recorded — first run" → nothing recorded, ever.

### Component 2 — mid-run rolls will be silently skipped in the future (the latent poison)

- Roll detection ONLY runs in `DataManager.initialize()` (startup). IBKR's continuous contract (ContFuture) switches basis mid-run; post-switch live bars are appended to the cache on the new basis with no ratio recorded.
- At the next restart the front-month mismatch IS detected, but `_compute_roll_ratio()` (data_manager.py:995-1050) compares the last 50 overlapping bars of a 3-day NOW-anchored IBKR fetch against the cache — after a mid-run switch both sides are post-roll basis → ratio ≈ 1.0 → "within tolerance — no adjustment needed" → seam permanently unrecorded.
- First live exposure: CL rolls ~2026-07-20 (front month currently CLQ6, per `.roll_metadata.json`).

## Evidence (all verified 2026-07-10)

1. **Timestamp/volume parity is perfect** — live 1h caches vs training HourSets share 4,500–4,730 exactly-matching timestamps per symbol, volume ratio 1.000 (same Databento source). No timezone/gap/dupe/NaN/dtype issues in any cache. The ONLY divergence is price basis.
2. **Adjustment method confirmed multiplicative (ratio), not Panama/difference:** within each roll segment, `HourSet_close / raw_close` is constant to CV ≈ 2e-8 and steps only at roll dates, converging to 1.0 in the newest segment; `HourSet − raw` drifts with price.
3. **Seam sizes in the live (raw) series that training never saw:** NG 2026-01-25 **−31%**, NG Apr-26 +6.4%, NG May-22 +4.2%, NG Jun-24 +1.4%; CL monthly 0.2–2.8%; ES quarterly ~0.9% (Jun-17 seam inside current 840h window); GC/SI ~0.8–1.2% bi-monthly (SI Jun-29 seam inside current windows).
4. **Measured prediction impact** (deployed model pickles re-run on raw vs training-basis windows, last 48 bars; raw-basis reconstruction validated against `shadow_log` probs in `fleet_telemetry.db` to mean |d| 0.0004–0.026):
   - NG (`NG01B_Sharpe_E03_07052026`): 10/48 SHORT signal flips (raw suppressed shorts: 3 fired vs 9 on training basis) + 3/48 LONG flips, while NG fell ~12% — raw basis inflates 35d vol baseline (TS_VOL_YZ_ZSCORE_72v840: −0.52 raw vs +3.61 adjusted).
   - SI: 1/48 flips (minor). CL/ES/GC: 0/48 flips currently (but distortion grows after every future roll; and CL's ~Jun-18 seam could not be measured — training data ends Jun-12).
   - NOT explained by this bug: GC/ES losses (long thresholds 0.34/0.30 put the model above threshold 48/48 bars on BOTH bases — separate model-selection issue; out of scope for this ticket).
5. **Per-roll correction factors are exactly recoverable** from `HourSet_close ÷ raw_close` segment quotients, with step timestamps as roll cutoffs. Example, NG cumulative factor walk (oldest→newest): 0.90470 → 0.82530 → 0.69051 → 0.67821 → 0.77196 → 1.08643 → 1.11749 → 1.12604 → 1.05931 → 1.01334 → 1.00000 (11 segments; per-roll ratio = next/prev cumulative). CL has 10 segments, ES 4, GC/SI similar.

## Affected artifacts

- `src/live_execution/data_manager.py` — `get_ratio_adjusted_df`, `_detect_rollover`, `_compute_roll_ratio`, roll metadata load/save.
- `src/live_execution/live_trader.py` — inference path (4325), warmup path (3272), live rollover handling (~3680) which updates `front_month_id` but never triggers ratio capture.
- `C:\CL_Analyst_Data\data\processed\.roll_metadata*.json` — empty `roll_history` for CL/ES/NG/GC/SI.
- Seeds `<SYM>_raw_1h.parquet` / caches `warm_start_cache*_1h.parquet` — raw basis with embedded seams.

## Remediation candidates surfaced during investigation (auditor to evaluate/design)

A. **Backfill `roll_history`** per symbol with the historical seam ratios+cutoffs derived from `HourSet ÷ raw` (one-time migration script + validation that `get_ratio_adjusted_df()` output matches the HourSet basis on overlap). Least invasive; cache stays raw; execution path untouched.
B. **Re-stage seeds from the ratio-adjusted series** and rebuild caches (basis = training as of seed end; future rolls via JIT). More invasive; touches execution-parity assumptions (cache would no longer be raw — bracket ATR reads the same rolling window).
C. **Close the mid-run roll gap** (component 2): capture the ratio at the moment the live rollover monitor detects the front-month change (live_trader ~3680), or persist front-month at detection time so a restart-based ratio computation still has pre-roll cache basis to compare against. Must land before ~2026-07-20 (next CL roll).

Note: fleet is RUNNING from this checkout (project rule: never edit the working tree while children run — coordinate restart timing); any fix must respect the no-silent-null-defaults rule and requires canary validation before production per user rules.
