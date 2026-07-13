# Auditor Design Proposal — ng-03b-calendar-spread-dataset_07122026_0249

**Role:** Ticket-Auditor (performed inline by Ticket-Manager; native subagent protocol unavailable)
**Ticket type:** FEATURE-DESIGN (not bug triage). "Severity" below classifies implementation scope, per workflow convention.
**Classification:** MEDIUM — multi-file additive change (1 new module + 3 localized edits + 2 new configs), gated behind a default-off flag. NOT a refactor; no interface/signature changes.

## Feature Request
Generate NG dataset `HourSet_03B` = `HourSet_02B` feature set PLUS futures-curve calendar-spread
features (`CURVE_*` prefix), so a new model can be trained and A/B-backtested against 02B to test
whether term-structure information improves NG models. Offline train/backtest only — live-trading
plumbing explicitly OUT OF SCOPE.

## Verified Facts (all re-checked against code this session)

| # | Claim | Verified at |
|---|-------|-------------|
| 1 | `_resolve_symbols()` passes through any symbol containing a dot → `NG.c.0`/`NG.c.1` fetchable with the existing CLI, zero code changes | `src/data/databento_data_builder.py:74-87` |
| 2 | Multi-symbol batch jobs interleave rows into ONE CSV (per-symbol validation harder) → submit one job PER LEG | `run_canary_test` docstring, `databento_data_builder.py:326-331` |
| 3 | `parse_raw_csv()` yields UTC `ts_event`, prices `/1e9`, `instrument_id` + `is_roll` — reusable as the leg loader, raw (unadjusted) by construction | `databento_data_builder.py:490-519` |
| 4 | Main bar index is naive-UTC (NG.csv written via `strftime` of UTC `ts_event`) → concurrent tz-naive join is correct; NO publication-lag shift needed (unlike COT +3BD) | `_to_pipeline_format` :653-664; `macro_features.py:660-666, 745-752` |
| 5 | Config-driven build path is `process_from_config()`; the curve merge slots in as a new step after the external-macro merge (Step 4) | `src/data_processor.py:3336-3488` |
| 6 | `FeatureConfig` (pydantic) holds the `include_*` flags; new fields are additive; existing DataMaps unaffected by a documented `False` default | `src/config/schemas.py:29-53` |
| 7 | `cleanup()` global-ffills non-target cols then `dropna` → any curve NaN surviving the engine would either be silently ffilled (violates house rule) or drop rows (breaks 02B/03B row alignment). Engine MUST guarantee NaN-free output after a bounded, documented ffill, else raise | `data_processor.py:727-745` |
| 8 | `get_feature_columns` excludes only `RAW_/TARGET_/META_/EXEC_` + OHLCV → `CURVE_*` flows into the model feature matrix automatically | `src/util.py:165-198` |
| 9 | The v2 cloud sweep (`vm_e2e_pipeline.py`) does NOT toggle buckets; bucket search only runs under `--use-buckets` (production path `vm_production_run.sh:228`). Registration is still mandatory hygiene: unclassified prefixes are silently always-on in bucket mode (`get_active_features` includes unknowns) | `feature_buckets.py:91-115`; grep of `gcp/` |
| 10 | The "time"-bucket pitfall is real: bucket lists `Hour_`/`DayOfWeek_` but actual columns are `Time_Sin`/`Time_Cos`/`Time_DayOfWeek_*` → always-on. The new prefix must match real column names exactly | `feature_buckets.py:33`; `data_processor.py:245-251` |
| 11 | GCS dataset name derivation: `dataset_version=HourSet_03B` + symbol NG → `NG_HourSet_03B.parquet`; preflight gate checks `gs://cltrainer-optuna-results/data/<name>` exists before any VM (missing file otherwise = STARTUP_TIMEOUT) | `vm_e2e_pipeline.py:1770-1780`; `run_sweep_batch.ps1:519-549` |
| 12 | 02B scout manifest is clean at HEAD (earlier dirty-status snapshot was stale); optuna box: n_trials 100, post_optimizer_trials 50 (pass-1), post_optimizer_ensemble_trials 150 (pass-2), holdout 12mo, seed 42, 6 target experiments | `configs/batch_manifest_v2_ng_hourset02b_scout.json` |
| 13 | Local NG data: `C:\CL_Analyst_Data\data\raw\DataBento\NG\` (raw job + `NG_raw/ratio/panama.csv`), staged `raw\NG.csv` (ratio); processed `NG_HourSet_02B.parquet` exists. No calendar-ranked leg data exists locally → the Databento pull is necessary | dir listing |
| 14 | NG canary template exists: `configs/batch_manifest_v2_ng_hourset01b_canary.json` (n_trials 3, 2 experiments) | glob |

## Proposed Design

### A. Data acquisition — existing CLI only, zero code changes
```powershell
# 1. Cost estimate first (free; report before submitting — grab-data rule)
conda run -n trader python -m src.data.databento_data_builder estimate --symbols NG.c.0 NG.c.1 --start 2010-06-06

# 2. Submit ONE JOB PER LEG (interleaving gotcha, Fact 2)
conda run -n trader python -m src.data.databento_data_builder submit --symbols NG.c.0 --start 2010-06-06 --outdir C:\CL_Analyst_Data\data\raw\DataBento\NG_c0
conda run -n trader python -m src.data.databento_data_builder submit --symbols NG.c.1 --start 2010-06-06 --outdir C:\CL_Analyst_Data\data\raw\DataBento\NG_c1
```
- Calendar-ranked (`.c.`) chosen over volume-ranked (`.v.`): volume ranks flip back and forth during
  roll week, making a v-ranked spread ill-defined exactly when the curve is most interesting.
- NO `convert` step: the engine consumes the raw Databento CSVs directly via `parse_raw_csv`.
  RAW leg prices only — back-adjustment is never applied to spread inputs (house rule from ticket).
- GLBX.MDP3 starts 2010-06-06; NG.csv history is the same epoch, so leg coverage spans the dataset.

### B. New module: `src/features/curve_features.py` — `CurveFeatureEngine`
Mirrors `MacroFeatureEngine`'s shape (explicit paths → build features on own timeline → tz-naive
`reindex(bar_times, method='ffill')` merge), minus the publication-lag shift (concurrent market data).

- `__init__(front_leg_csv, second_leg_csv)` — explicit paths; `FileNotFoundError` with remediation
  text if missing (no auto-download in v1).
- `_build_curve_features() -> pd.DataFrame` (indexed by naive-UTC timestamp):
  1. Load both legs with `DatabentoDataBuilder.parse_raw_csv` (reuse, don't reimplement);
     `ts_event` → tz-naive UTC.
  2. INNER-join legs on exact timestamp → `F1` = front close, `F2` = second close. Only concurrent
     bars produce a spread — never pair a stale leg against a fresh one.
  3. `spread_pct = (F2 - F1) / F1`  (contango ⇒ positive).
  4. Features (6, computed on the joined-timestamp timeline):
     - `CURVE_CONTANGO_SIGN` = sign(F2 − F1), int {−1, 0, +1}
     - `CURVE_SPREAD_PCT` = spread_pct (% of front — the raw dollar level is deliberately NOT
       emitted; % of front carries less expiry sawtooth and is scale-free across NG price regimes)
     - `CURVE_SPREAD_PCT_Z_24 / _Z_72 / _Z_168` = rolling z-scores over the existing 24/72/168
       windows. Zero-std guard: where rolling std == 0, z := 0.0 (constant spread = exactly at its
       mean) — explicit and documented, never inf/NaN.
     - `CURVE_SPREAD_PCT_WOW` = spread_pct − spread_pct.shift(168) (house "1W = 168 bars" convention,
       matching `macro_windows`).
  5. Roll diagnostics (log-only in v1): detect front-leg rolls via `instrument_id` change; log roll
     count and mean |Δspread_pct| across roll boundaries. No masking/normalization in v1 (see Open
     Questions).
- `merge_curve(df, max_leading_nan_bars) -> pd.DataFrame`:
  1. Validate `DatetimeIndex`, monotonic (same guards as `merge_all`).
  2. `reindex(bar_times, method='ffill', limit=FFILL_LIMIT_BARS)` with module constant
     `FFILL_LIMIT_BARS = 24` (one trading day) — the documented forward-fill bound.
  3. **Fail-loud validation (house rule — NO silent null defaults):**
     - Leading-NaN region (before the first joined leg bar) must be ≤ `max_leading_nan_bars`
       (`process_from_config` passes the warmup budget, 2200) — else `ValueError`.
     - After the first valid bar, ZERO residual NaN allowed. Any survivor means a leg gap
       > 24 bars during hours the front market traded → `ValueError` listing the gap timestamps
       and instructing the operator to inspect coverage / consciously raise the limit.
     - Log a coverage report: joined-bar count, % of bar index covered pre-ffill, max gap filled.

### C. Schema: `src/config/schemas.py` → `FeatureConfig` (additive)
```python
include_curve_spread: bool = False        # documented default; all existing DataMaps byte-identical
curve_front_leg_csv: Optional[str] = None
curve_second_leg_csv: Optional[str] = None
```
`model_validator(mode="after")`: raise if `include_curve_spread=True` and either path is
missing/blank; ALSO raise if paths are set while the flag is False (a half-configured DataMap must
crash, not silently ignore the paths).

### D. Pipeline hook: `src/data_processor.py` → `process_from_config` Step 4.5
Immediately after the external-macro merge (line ~3384):
```python
if cfg.features.include_curve_spread:
    from src.features.curve_features import CurveFeatureEngine
    curve_engine = CurveFeatureEngine(cfg.features.curve_front_leg_csv, cfg.features.curve_second_leg_csv)
    df = curve_engine.merge_curve(df, max_leading_nan_bars=warmup_budget)
    print(f"  [58%] {N} curve features added ...")
```
No other touchpoints: `normalize_features` ignores `CURVE_*`; `cleanup`'s global ffill is a no-op
given the engine's NaN-free guarantee; `get_feature_columns` includes `CURVE_*` automatically (Fact 8).

### E. Bucket registration: `src/features/feature_buckets.py`
```python
"curve": ["CURVE_"],   # futures-curve calendar-spread features (contango sign, spread z-scores)
```
Prefix matches the actual column names exactly (avoids the Time_* pitfall, Fact 10).
`TOGGLEABLE_BUCKETS` auto-derives. `tests/test_feature_buckets.py` gets classify + toggle cases.

### F. DataMap: `configs/master/DataMap_NG_HourSet_03B.json`
Copy of `DataMap_NG_HourSet_02B.json` changing ONLY:
- `dataset_version`: `HourSet_03B`; `output_filename`: `NG_HourSet_03B.parquet`
- `features.include_curve_spread: true`
- `features.curve_front_leg_csv` / `curve_second_leg_csv`: the two per-leg Databento CSVs from step A
- Everything else (windows, macro_windows, include_*, drop_features, targets) **byte-identical** —
  clean A/B attribution demands the delta be exactly the 6 CURVE_ columns.

### G. Build sequence + hard gates
1. **Control-regression gate (mandatory, runs FIRST):** rebuild 02B with the new code
   (`include_curve_spread` absent → False) and assert equality with the existing
   `NG_HourSet_02B.parquet` (same index, same columns, same values). Proves the shared pipeline
   is untouched for every existing dataset. (House pattern: byte-identical guard on shared code.)
2. Build 03B:
   ```powershell
   conda run -n trader python scripts/regenerate_features.py --config configs/master/DataMap_NG_HourSet_03B.json --exec-data C:\CL_Analyst_Data\data\raw\DataBento\NG\NG_raw.csv
   ```
3. **A/B parity gate (scratchpad script):**
   - `03B.index` EXACTLY equals `02B.index` (any row drift = curve NaNs leaked into `dropna` —
     hard fail, invalidates the A/B);
   - all `TARGET_*` columns identical to 02B (targets must be untouched);
   - `set(03B.columns) == set(02B.columns) + the 6 CURVE_*`;
   - zero NaN; `CURVE_CONTANGO_SIGN ∈ {−1,0,1}`; spread distribution sanity (NG mostly contango,
     winter backwardation episodes visible).
4. **Upload BEFORE any batch reference** (STARTUP_TIMEOUT gotcha; local gsutil broken → gcloud):
   ```powershell
   gcloud storage cp C:/CL_Analyst_Data/data/processed/NG_HourSet_03B.parquet gs://cltrainer-optuna-results/data/NG_HourSet_03B.parquet
   gcloud storage ls -l gs://cltrainer-optuna-results/data/NG_HourSet_03B.parquet
   ```
   (`NG_raw.parquet` execution data is already in GCS — unchanged.)

### H. A/B test plan
1. **Canary first** (house rule: canary before scout/prod after ANY pipeline change — and this
   changes `process_from_config`): `configs/batch_manifest_v2_ng_hourset03b_canary.json` mirroring
   `..._ng_hourset01b_canary.json`, only `dataset_version`/labels/prefixes/_comment changed
   (n_trials 3, 2 experiments). Human authorizes the VM spend.
2. **Scout A/B**: `configs/batch_manifest_v2_ng_hourset03b_scout.json` mirroring
   `configs/batch_manifest_v2_ng_hourset02b_scout.json` @ HEAD, changing ONLY:
   `dataset_version` → `HourSet_03B`, experiment `label`s → "NG HS03B …", `gcs_prefix`es →
   `sweep_ng_hs03b_*_scout`, `_comment`. Preserved verbatim: seed 42, the same 6 target families
   (2x1_1H, 2x1_2H, 5x1_6H, 4x1_6H, 2x1_3H, 3x1_6H), optuna box (n_trials 100 / post_optimizer 50
   pass-1 / ensemble 150 pass-2 / holdout 12mo), infra, slippage 0.001,
   `execution_data_path=gs://.../NG_raw.parquet`.
3. **Evaluation — model-level primary** (the NG 02B verdict showed ensemble PnL is confounded by
   pair-selection collapse):
   - Primary: per-target holdout AUC + solo-model holdout PnL, 03B vs 02B like-for-like;
   - Feature-importance check: do CURVE_* features actually rank (are they used at all)?
   - Secondary: ensemble holdout PnL (with the pair-selection caveat noted in the report);
   - Optional (only if 03B looks promising): seed-43 rerun for pair-stability, per the 15B lesson.

## Target Files (future TDD work)
| File | Change |
|------|--------|
| `src/features/curve_features.py` | NEW — `CurveFeatureEngine` |
| `src/config/schemas.py` | `FeatureConfig`: 3 additive fields + validator |
| `src/data_processor.py` | `process_from_config`: gated Step 4.5 |
| `src/features/feature_buckets.py` | add `"curve": ["CURVE_"]` |
| `tests/test_curve_features.py` | NEW — see test list in blueprint |
| `tests/test_feature_buckets.py` | curve-bucket classify/toggle cases |
| `configs/master/DataMap_NG_HourSet_03B.json` | NEW config |
| `configs/batch_manifest_v2_ng_hourset03b_canary.json` | NEW config |
| `configs/batch_manifest_v2_ng_hourset03b_scout.json` | NEW config |

## Leakage analysis
- Concurrent market data → no publication-lag shift (correct to differ from COT's +3BD here).
- `ts_event` = interval start on BOTH sides (same Databento source/schema) → joining leg close at
  bar T to main bar T matches the pipeline-wide convention that a bar's features are consumed at
  its close for subsequent decisions. No relative lookahead.
- z-scores/WoW are backward-looking rolls/shifts. ffill only (never bfill).
- Required unit test: mutating leg data strictly AFTER bar T must not change any CURVE_ value at
  or before T.

## Alternatives considered (and why not)
1. **Reuse existing `NG_raw.csv` (NG.v.0) as the front leg; fetch only `NG.c.1`** — REJECTED:
   volume-rank flips during roll week make a v0-vs-c1 spread ill-defined (occasionally the same
   contract, spread ≡ 0, or inverted legs); c.0+c.1 is self-consistent and the extra c.0 pull is
   nearly free at ohlcv-1h.
2. **Time-to-expiry normalization of the spread** — DEFERRED to a possible 03C: requires an expiry
   calendar dependency; the chosen sign/z/WoW set already suppresses most sawtooth (the raw dollar
   level, the worst offender, is not emitted). Impact-Reviewer to confirm or overrule.
3. **AlphaFactory flag instead of a separate engine** — REJECTED: AlphaFactory is single-series
   OHLCV; curve features need a second data source. `MacroFeatureEngine` is the established
   pattern for external-source merges.
4. **Databento `spread` instruments (e.g. NG calendar-spread symbols) instead of leg math** —
   REJECTED for v1: different symbology/liquidity, unverified in this stack; leg math over
   already-proven continuous symbols is the minimal-risk path.

## Severity & justification
MEDIUM. Additive, default-off, regression-gated; no signatures change; blast radius on shared code
(`data_processor`, `schemas`, `feature_buckets`) is controlled by the 02B byte-parity gate and the
default-False flag. No business-justification exception needed (no interface/base-class breakage).
