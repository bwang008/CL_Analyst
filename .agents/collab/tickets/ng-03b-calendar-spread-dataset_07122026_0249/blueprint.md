# Ticket Resolution Blueprint — ng-03b-calendar-spread-dataset_07122026_0249 (v2)
**Ticket Directory:** `.agents/collab/tickets/ng-03b-calendar-spread-dataset_07122026_0249/`
**Type:** FEATURE-DESIGN (offline dataset + A/B; live-trading plumbing OUT OF SCOPE)
**Version:** v2 — revised per HUMAN review (full angle-coverage of the curve + NG seasonality) with
the coordinator's BINDING feasibility corrections R1–R8 incorporated. v1 superseded; all v1
"KEEP — do not regress" items preserved verbatim below. See `auditor_proposal.md` (v1 design +
verified facts), `impact_review.md` (v1 critique + v2 addendum).

## ⛔ Execution preconditions (KEEP — unchanged from v1)
1. The currently in-flight cloud batch has COMPLETED (never edit the tree mid-batch; code is
   zipped at optimizer-deploy time).
2. Human decisions (list at bottom) resolved.
3. This ticket changes `process_from_config` (and now `add_time_features`) → house rule: **canary
   run required before any scout/prod launch.**

## Feature Summary (v2)
Build NG dataset `HourSet_03B` = `HourSet_02B` + a **superset** of futures-curve calendar-spread
features (`CURVE_*`, 29 unconditional columns across 9 angle groups) + gated month-of-year cyclical
time encoding (`Time_Month_*`, 2 columns), computed from RAW (unadjusted) Databento calendar-ranked
legs `NG.c.0`/`NG.c.1` joined at concurrent timestamps. Philosophy (per human plan): generate the
superset and let the curve bucket toggle + `feature_fraction` + FI prune it — matching the
`add_term_structure_shapes` superset precedent. A null A/B result is then informative about the
term-structure CONCEPT, not under-coverage.

## Target Files
- `src/features/curve_features.py` — **NEW**: `CurveFeatureEngine` (all CURVE_* groups incl. seasonal)
- `src/config/schemas.py` — `FeatureConfig`: 6 additive fields + validators
- `src/data_processor.py` — `process_from_config` Step 4.5 (unchanged pattern); `add_time_features`
  gains additive `include_month: bool = False` param
- `src/features/feature_buckets.py` — add `"curve": ["CURVE_"]`; FIX `"time"` prefixes → `["Time_"]`;
  update stale `BUCKET_MIN_TRIALS` comment (13 toggleable buckets → 2^13 combos)
- `tests/test_curve_features.py` — **NEW** (mandatory tests below)
- `tests/test_feature_buckets.py` — rewrite the time-bucket cases (existing :48-49 assert the
  fictional `Hour_sin`/`DayOfWeek_cos` prefixes — they encode the bug); add Time_Month_* + CURVE_* cases
- `configs/master/DataMap_NG_HourSet_03B.json` — **NEW**
- `configs/batch_manifest_v2_ng_hourset03b_canary.json` — **NEW**
- `configs/batch_manifest_v2_ng_hourset03b_scout.json` — **NEW**

## Required Changes

### 0. Data pull (KEEP — unchanged from v1; existing CLI, ZERO code changes)
```powershell
conda run -n trader python -m src.data.databento_data_builder estimate --symbols NG.c.0 NG.c.1 --start 2010-06-06
# ONE JOB PER LEG (multi-symbol jobs interleave one CSV):
conda run -n trader python -m src.data.databento_data_builder submit --symbols NG.c.0 --start 2010-06-06 --outdir C:\CL_Analyst_Data\data\raw\DataBento\NG_c0
conda run -n trader python -m src.data.databento_data_builder submit --symbols NG.c.1 --start 2010-06-06 --outdir C:\CL_Analyst_Data\data\raw\DataBento\NG_c1
```
No `convert`; never back-adjust legs. Then the leg-coverage scan (feeds H3).

### 1. `src/features/curve_features.py` — `CurveFeatureEngine` (v2 feature inventory)
Base construction (KEEP): load legs via `DatabentoDataBuilder.parse_raw_csv`, tz-naive UTC,
INNER-join at concurrent timestamps → `F1` (front close), `F2` (second close);
`spread_pct = (F2 − F1) / F1`. All features computed on the JOINED timeline, then merged
(§1c). Docstring MUST state: (a) `CURVE_` is strictly separate from the `TS_`/`term_structure`
cross-window indicator features; (b) true curve CURVATURE requires `NG.c.2` — explicitly out of
scope for 03B.

**BINDING signed-series rule (R3):** `spread_pct` crosses zero → it is a SIGNED series under the
house rule (`alpha_factory.py:765-767`): Diff / Sign_Agreement / Regime_Cross / ZScore families
only — **NO `pct_change`, NO Ratio/Log_Ratio/Invert on `spread_pct` itself.** The ratio angle is
covered by `VOLRATIO` (spread-vol is a positive series — ratio legal, clip denom `1e-8`) and
`Z_DIFF`. **Do NOT call or modify `add_term_structure_shapes`** (R1): its `TS_` output prefix is
hardcoded (`alpha_factory.py:868-912`) and would land curve features in the wrong bucket; mirror
the needed shape logic locally with `CURVE_` naming. (Sign_Agreement/Regime_Cross mirrors are
legal-if-desired but intentionally NOT emitted — the enumeration below is the approved set.)

| # | Group | Column | Definition |
|---|-------|--------|------------|
| 1 | Level/state | `CURVE_SPREAD_PCT` | spread_pct (raw DOLLAR spread stays out — KEEP) |
| 2 | Level/state | `CURVE_CONTANGO_SIGN` | sign(F2 − F1), int {−1, 0, +1} |
| 3–7 | Velocity | `CURVE_SPREAD_ROC_{1,3,6,12,24}` | `spread_pct.diff(n)` — **simple differences, NOT pct_change (R3)** |
| 8–9 | Velocity | `CURVE_SPREAD_SLOPE_{24,72}` | `_rolling_slope_r2_numba` slope (`alpha_factory.py:113`). NO Close-division: spread_pct is already unitless %/bar (deviation from `TREND_LR_SLOPE`'s slope/Close normalization — document in docstring) |
| 10–11 | Velocity | `CURVE_SPREAD_SLOPE_R2_{24,72}` | R² from the same call |
| 12 | Acceleration | `CURVE_SPREAD_ACCEL_24` | second difference: `ROC_24 − ROC_24.shift(24)` (signed-legal; slope-of-slope variant rejected as needless complexity) |
| 13–18 | Anchor/gap | `CURVE_SPREAD_PCT_Z_{24,72,168,336,840,2160}` | rolling z; **house windows (R4): 840 ≈ 1M, 2160 ≈ 3M — not 720**; std==0 ⇒ z := 0.0 (KEEP) |
| 19 | Anchor/gap | `CURVE_SPREAD_DIST_MEAN_840` | `spread_pct − rolling_mean_840` (Diff, not Ratio — signed) |
| 20 | Anchor/gap | `CURVE_SPREAD_PCTL_840` | `spread_pct.rolling(840).rank(pct=True)` — **MUST use the native Rolling.rank pattern (R5; `macro_features.py:616`; the MACRO_PCTILE 339x hotspot fix c1c78fc — never a naive apply-rank)** |
| 21 | Cross-TF regime | `CURVE_SPREAD_Z_DIFF_24v840` | `Z_24 − Z_840` (fast z minus slow z; ZScore/Diff mirror of the TS shape logic) |
| 22 | Cross-TF regime | `CURVE_SPREAD_VOLRATIO_24v840` | `VOL_24 / VOL_840.clip(lower=1e-8)` (`CROSS_VOL_RATIO` pattern, `alpha_factory.py:722-727`; positive series → ratio legal) |
| 23–24 | Spread vol | `CURVE_SPREAD_VOL_{24,168}` | rolling std of spread_pct (emit the z denominators; an unemitted VOL_840 is reused internally by VOLRATIO) |
| 25 | Spread vol | `CURVE_SPREAD_VOLVOL_168` | `VOL_168.rolling(168).std() / VOL_168.rolling(168).mean()` (`VOL_VOLVOL` CoV pattern, `alpha_factory.py:303-306`) |
| 26 | Long change | `CURVE_SPREAD_WOW` | `spread_pct − spread_pct.shift(168)` (KEEP from v1) |
| 27 | Long change | `CURVE_SPREAD_MOM_840` | `spread_pct − spread_pct.shift(840)` (house 1M window, R4) |
| 28 | Roll context | `CURVE_BARS_SINCE_ROLL` | bars since last front-leg roll (`instrument_id` change from `parse_raw_csv`'s `is_roll`; backward-looking only; lineage precedent `scripts/backfill_roll_history.py`). Old-H2 deferral REVOKED — now in scope |
| 29 | Seasonality | `CURVE_SPREAD_SEASONAL_Z` | see §1b |
| (c1) | Level/state | `CURVE_ROLL_YIELD` | `spread_pct / days_to_front_expiry` — **ONLY if a contractual days-to-expiry series is configured (H-rollyield)**. Verified: no historical expiry calendar exists training-side today → DEFAULT: defer to 03C and **log the deferral loudly** (never silently omit). "Bars to next OBSERVED roll" is an ILLEGAL substitute — lookahead (R8) |
| (c2) | Seasonality | `CURVE_SPREAD_SEASONAL_PCTL` | optional companion (prior-only percentile within the seasonal bucket) — human opt-in (H-seasonal-2) |

**Count: 29 unconditional `CURVE_*` + 2 `Time_Month_*` (§2b) = 31 new columns default; up to 33
with (c1)/(c2) opt-ins. The A/B parity gate pins the EXACT enumerated set (§7.3).**

### 1b. `CURVE_SPREAD_SEASONAL_Z` — prior-only seasonal anomaly (NEW, in scope)
- Bucket key: **week-of-year** (`index.isocalendar().week`) by default; alternatives month or
  ±2-week smoothed day-of-year — human decision H-seasonal-1. Week 53: kept separate by default
  (the prior-years gate handles sparsity); merging 53→52 is a surfaced option under H-seasonal-1 (R8).
- **CAUSALITY (mandatory construction):** within each seasonal group, in strict time order:
  `mean = group.expanding().mean().shift(1)`, `std = group.expanding().std().shift(1)` —
  prior-only; no row may see data at or after its own timestamp.
  `SEASONAL_Z = (spread_pct − mean) / std`.
- **Cold start:** a row requires ≥ `curve_seasonal_min_prior_years = 2` DISTINCT PRIOR YEARS of
  same-bucket history (distinct-year count, NOT observation count) — else emit **0.0 (neutral)**.
  Same 0.0 for std==0. **NEVER drop rows** (index-parity gate depends on it). This 0.0 neutral
  fill and the zero-std z are the ONLY exceptions to fail-loud (R6); everything else keeps the
  fail-loud policy.

### 1c. `merge_curve` (KEEP — v1 policy verbatim, restated)
- Same index guards as `merge_all`; `reindex(bar_times, method='ffill', limit=FFILL_LIMIT_BARS)`;
  `FFILL_LIMIT_BARS = 24` provisional pending the coverage scan (H3), documented per
  no-silent-nulls.
- Fail-loud: leading-NaN run > `max_leading_nan_bars` ⇒ `ValueError`; ANY residual NaN after the
  first valid bar ⇒ `ValueError` listing gap timestamps; log the coverage report.
- **Warmup reasoning (R6, state in docstring):** curve windows top out at 2160 bars, below the
  existing 4320-bar macro max, so wherever 02B rows exist the curve windows have sufficient
  PRIMARY history (the 02B parquet's first surviving row already post-dates the 4320-bar warmup;
  the curve leading-NaN region ends ~2160 joined bars after the 2010-06 leg start — well before
  it). Residual NaN risk comes ONLY from NG.c.1 leg coverage, governed by the ffill-limit +
  fail-loud policy and the H3 scan. The 0.0 neutral fill applies ONLY to the seasonal cold-start
  and zero-std cases — everything else keeps fail-loud. The index-parity gate (§7.3) is the hard
  arbiter.

### 2. `src/config/schemas.py` — `FeatureConfig` (additive; all existing DataMaps byte-identical)
```python
include_curve_spread: bool = False
curve_front_leg_csv: Optional[str] = None
curve_second_leg_csv: Optional[str] = None
include_month_encoding: bool = False                                  # §2b (R2)
curve_seasonal_bucket: Literal["week", "month", "doy_smoothed"] = "week"
curve_seasonal_min_prior_years: int = 2      # validator: >= 2
curve_seasonal_pctl: bool = False            # optional companion column (c2)
```
Validators (KEEP v1 hygiene + extend): flag True with missing leg paths ⇒ raise; leg paths set
while flag False ⇒ raise; any non-default `curve_seasonal_*` while `include_curve_spread` False ⇒
raise (no half-states). `include_month_encoding` is independent (time feature, not curve-coupled).

### 2b. Month-of-year cyclical encoding (`Time_Month_Sin/Cos`) — GATED (R2, binding)
`Time_Month_Sin/Cos = sin/cos(2π·(month−1)/12)`, added in `add_time_features` behind a NEW additive
param `include_month: bool = False`; `process_from_config` passes
`cfg.features.include_month_encoding`. **Rationale (R2):** adding it unconditionally in the shared
cyclical-time step would break the rebuild-02B byte-parity control gate (every rebuilt dataset
would gain columns). 03B sets the flag True → `Time_Month_*` ships as part of the 03B bundle.
Optional day-of-year alternative encoding — human decision H-seasonal-3 (default: month).

### 3. `src/data_processor.py` (KEEP v1 shape)
- Step 2: `add_time_features(df, include_day_of_week=True, include_month=cfg.features.include_month_encoding)`.
- Step 4.5 after the macro merge (~line 3384): unchanged v1 hook —
  `CurveFeatureEngine(...).merge_curve(df, max_leading_nan_bars=2200)` behind
  `cfg.features.include_curve_spread`.
- `normalize_features` ignores `CURVE_*`/`Time_Month_*`; `get_feature_columns` includes them
  automatically (verified `src/util.py:165-198`).

### 4. `src/features/feature_buckets.py`
- Add `"curve": ["CURVE_"]` (exact-match against every emitted name in §1's table — verified no
  collisions; avoids the always-on pitfall).
- **FIX the time bucket (§S1):** `"time": ["Time_"]` — the current `["Hour_", "DayOfWeek_"]`
  prefixes never match the real `Time_Sin`/`Time_Cos`/`Time_DayOfWeek_*` columns, silently making
  them always-on in bucket mode. `Time_Month_*` then classifies as "time" automatically.
- Update the stale `BUCKET_MIN_TRIALS` comment (now 13 toggleable buckets → 2^13 combos).
- NOTE (R7): this fix changes the Optuna bucket-mode search space at the new HEAD (time features —
  currently top-2 by FI — become genuinely toggleable) → drives H-ab-code-parity below. The fix
  itself does NOT change dataset bytes (training-side only) — safe for the §7.1 parity gate.

### 5. Mandatory tests
`tests/test_curve_features.py` (synthetic two-leg fixtures):
- Math per column group: sign; spread_pct; ROC diffs (assert NOT pct_change semantics on a
  zero-crossing fixture); slope/R² vs closed form on a linear fixture; ACCEL second difference;
  z incl. std==0 ⇒ 0.0; DIST_MEAN; PCTL (vs naive rank on a small fixture); Z_DIFF; VOLRATIO
  (+denominator clip); VOL/VOLVOL; WOW/MOM; BARS_SINCE_ROLL across a synthetic instrument_id roll.
- **No-lookahead mutation test covering EVERY new column incl. SEASONAL_Z:** mutate leg rows AND
  same-week rows strictly after T ⇒ all values at/before T byte-identical.
- **Seasonal causality fixture:** a case where including future years WOULD change the value —
  assert it does not (expanding+shift(1) proof).
- Seasonal cold-start: <2 distinct prior years ⇒ exactly 0.0, no NaN, no row drop; distinct-YEAR
  counting (many observations in a single prior year still ⇒ 0.0).
- Merge policy (KEEP): gap > limit raises listing timestamps; leading-NaN budget; tz/monotonic
  guards.
- ROLL_YIELD deferral: engine logs the loud deferral line when no expiry series configured.
- `Time_Month_Sin/Cos` values at known dates (Jan/Jul); flag-off ⇒ columns absent.
`tests/test_feature_buckets.py`: `classify_feature("Time_Month_Sin") == "time"`,
`classify_feature("Time_Sin") == "time"`, `classify_feature("CURVE_SPREAD_SEASONAL_Z") == "curve"`;
REWRITE the `Hour_sin`/`DayOfWeek_cos` assertions (:48-49 — they encode the bug); toggle
inclusion/exclusion via `get_active_features`.
Schema validator raise-cases from §2. Full suite green vs pre-ticket baseline.

### 6. `configs/master/DataMap_NG_HourSet_03B.json`
Copy `DataMap_NG_HourSet_02B.json`, change ONLY: `dataset_version`/`output_filename` (03B),
`include_curve_spread: true`, the two leg-CSV paths, `include_month_encoding: true`, and the
`curve_seasonal_*` values resolved by H-seasonal-1/2. Windows, macro_windows, other include_*,
drop_features, targets: **byte-identical** (KEEP).

### 7. Build + hard gates (KEEP — v1 verbatim except gate-3 count)
1. **Control-regression gate:** rebuild 02B with new code (all new flags default-off) to a
   **SCRATCH output path** — NEVER in-place (control-clobber hazard). Assert index+columns+values
   equal to the untouched production `NG_HourSet_02B.parquet`. This gate is also WHY R1/R2 forbid
   unconditional changes to `add_term_structure_shapes`/`add_time_features` outputs.
2. Build 03B via `regenerate_features.py` (verify `--exec-data` against the 02B lineage artifact
   `data/processed/NG_HourSet_02B_config.json` first).
3. **A/B parity gate:** `03B.index` EXACTLY == `02B.index`; every `TARGET_*` identical to 02B;
   new columns == EXACTLY the enumerated set from §1/§2b as resolved by the human decisions
   (default N=31: 29 CURVE_* + 2 Time_Month_*; assert the LITERAL name list from the DataMap
   lineage, not just a count); zero residual NaN; `CURVE_CONTANGO_SIGN ⊆ {−1,0,1}`;
   `SEASONAL_Z == 0.0` for the entire cold-start era; spread-distribution sanity (NG mostly
   contango, winter backwardation episodes visible).
4. **Upload BEFORE any manifest references it** (KEEP): `gcloud storage cp … NG_HourSet_03B.parquet
   gs://cltrainer-optuna-results/data/` + `gcloud storage ls -l` verify.

### 8. A/B manifests + run plan (KEEP v1 + R7 addition)
- Canary first (pipeline code changed) — mirror `..._ng_hourset01b_canary.json`, dataset_version/
  labels/prefixes/_comment only; `-DryRun` gates arbitrate legacy fields.
- Scout — pure mirror of `configs/batch_manifest_v2_ng_hourset02b_scout.json` @ HEAD
  (dataset_version → HourSet_03B, labels "NG HS03B …", `gcs_prefix` `sweep_ng_hs03b_*_scout`,
  `_comment`). Same seed 42, same 6 target families, same optuna box (100 / 50 / 150 / 12mo).
  Manifest carries NO curve/month schema fields (VMs never rebuild features).
- **H-ab-code-parity (R7, NEW human decision):** the time-bucket fix + Time_Month columns + any
  HEAD drift mean the in-flight 02B scout is NOT code-identical to the 03B arm. Options:
  (a) **RECOMMENDED** — rerun the 02B arm at the same HEAD as 03B (+1 scout cost) for a clean
  code-identical A/B; (b) accept bundled attribution and label the A/B as testing the whole 03B
  concept (curve + seasonality + bucket fix). Human picks.
- Evaluation (KEEP + v2 note): model-level PRIMARY (per-target holdout AUC, solo holdout PnL, FI
  ranks of CURVE_*/Time_Month_*); ensembles SECONDARY (pair-selection-collapse caveat); trials not
  paired across arms (distribution-level comparison). **v2 framing:** the superset now covers all
  curve angles + seasonality, so a null result is informative about the term-structure concept
  rather than attributable to under-coverage. Optional seed-43 stability rerun if promising.

## Open questions / HUMAN decisions (blocking before TDD)
- **H1 (KEEP):** Databento spend for NG.c.0 + NG.c.1 full history (estimate → report → authorize).
- **H2 (REVISED):** `CURVE_BARS_SINCE_ROLL` is now IN SCOPE (old deferral revoked). Remaining
  sign-off: accept that Z/WOW/slope windows still carry roll-day spread jumps (no masking, no
  TTE-normalization in v1) — now partially mitigated by BARS_SINCE_ROLL giving the model
  roll-phase context.
- **H3 (KEEP):** Confirm `FFILL_LIMIT_BARS` (default 24) after the leg-coverage scan.
- **H4 (KEEP):** Canary + scout VM spend (canary strictly before scout).
- **H5 (KEEP):** All work starts only after the in-flight batch completes.
- **H-seasonal-1 (NEW):** Seasonal bucket — week-of-year (default) vs month vs ±2-week smoothed
  day-of-year; sub-option: merge ISO week 53 into 52 (default: keep separate).
- **H-seasonal-2 (NEW):** Confirm ≥2 distinct-prior-years cold-start gate + 0.0 neutral fill;
  opt in/out of the `CURVE_SPREAD_SEASONAL_PCTL` companion (default: out).
- **H-seasonal-3 (NEW):** Month encoding (default) vs day-of-year encoding for the `Time_*`
  cyclical addition.
- **H-rollyield (NEW):** `CURVE_ROLL_YIELD` needs a CONTRACTUAL days-to-expiry series; none exists
  training-side today (verified — only live-side roll timing in the instrument registry). Default:
  defer to 03C with a loud log. Alternative: commission an expiry-calendar generator (registry
  `active_months` + LTD/FND rules) if wanted in 03B. "Bars to next observed roll" is NOT an
  acceptable substitute (lookahead, R8).
- **H-ab-code-parity (NEW, R7):** rerun the 02B arm at the same HEAD as 03B (recommended, +1 scout
  cost) vs accept bundled attribution.
