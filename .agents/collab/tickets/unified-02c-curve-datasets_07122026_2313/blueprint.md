# Ticket Resolution Blueprint — unified-02c-curve-datasets_07122026_2313
**Ticket Directory:** `.agents/collab/tickets/unified-02c-curve-datasets_07122026_2313/`
**Status:** APPROVED by Impact-Reviewer (10 binding conditions). Phases 2 & 5 HARD-BLOCKED on
human authorization (D1 Databento spend, D6 VM spend). Execution may start with Phases 0–1
once the operator confirms the D-decisions below.

## Goal
Unified **HourSet_02C** dataset for all 8 symbols (CL/ES/NG/NQ/ZC/GC/ZS/SI):
each symbol's current B-level feature set + the 24-target set + 29 `CURVE_*` + 2 `Time_Month_*`
(the features NG's HourSet_03B introduced). NG's 03B is renamed to 02C (forward references only);
**NG 02C must be byte-identical to 03B** — that equality anchors the rename's safety.

## Verified foundations (Auditor + Reviewer, independently)
- `CurveFeatureEngine` is already symbol-agnostic: legs/seasonal bucket via per-symbol
  `FeatureConfig` fields in each DataMap; `process_from_config` path → **zero code changes needed
  for the 7 new DataMaps**. NG residues: `FFILL_LIMIT_BARS=24` (curve_features.py:74),
  `max_leading_nan_bars=2200` (data_processor.py:3415) — both fail loud; FROZEN per condition 5.
- Ladder: ES/GC/SI/NG at 02B; NQ/ZC/ZS at 01B (same features, +3 targets needed);
  CL 15B≡14B (+3 targets, keeps CL-only `continuous_return` — D3 default).
- Rename surface: `HourSet_03B|hs03b` exists only in configs/ (2 manifests + 1 DataMap) and
  comments/docs. Never promoted live; completed batches reference the GCS object (kept, D5).

## Phases
0. Confirm the 03B-era curve code commit (`git show --stat f7abd22`) contains the cloud-validated
   set; commit/resolve the in-tree scout-manifest diffs (restored 2x1_1H/5x1_6H arms) BEFORE any
   rename (condition 1). Never edit the tree while a batch is in flight.
1. **NG 03B→02C rename**: create `configs/master/DataMap_NG_HourSet_02C.json` (only
   version/filename changed), retire `DataMap_NG_HourSet_03B.json`; REBUILD via
   `scripts/regenerate_features.py` (never copy/hand-edit lineage); **byte-equality assert vs
   `NG_HourSet_03B.parquet` — HALT on mismatch** (condition 2); upload
   `gs://cltrainer-optuna-results/data/NG_HourSet_02C.parquet` + `gcloud storage ls` verify;
   manifests `..._ng_hourset03b_{canary,scout}.json` → `..._ng_hourset02c_{canary,scout}.json`
   (dataset_version, labels `NG HS02C`, gcs_prefix `sweep_ng_hs02c_*`); grep gate: zero
   `HourSet_03B|hs03b` under configs/. Historical batch dirs/reports/legacy parquet+GCS untouched.
2. **[HARD GATE D1] Databento acquisition** (~$10.65 est. total): GLBX.MDP3, ohlcv-1h, raw,
   one batch job per leg, 2010-06-06→T-1, `<SYM>.c.0`/`.c.1` for CL/ES/NQ/GC/SI/ZC/ZS into
   `C:\CL_Analyst_Data\data\raw\DataBento\<SYM>_c0|_c1\`. NG already owned.
3. **Leg-coverage scan** (free, local): join% / max gap / leading-NaN per symbol; decides D4
   (GC/SI second leg: c.1 vs c.2 vs v.1 — serial-month thinness). Condition 8: if gaps exceed the
   24-bar ffill limit, choose an alternative leg or exclude with human sign-off — NEVER loosen
   the limit to force a pass.
4. **Per-symbol 02C builds** (order per symbol): (a) flags-off control rebuild to scratch,
   byte-parity vs production B (condition 4); (b) new `DataMap_<SYM>_HourSet_02C.json` (version,
   filename, 5 curve/month fields, leg paths, +3 targets where needed); (c) build;
   (d) `scripts/verify_02c_parity.py` — NEW script, must assert: index == existing B index,
   **byte-identity of ALL shared columns (features AND targets)** (condition 3 — strengthened),
   new columns == the exact 31-name list, zero residual NaN; (e) GCS upload + ls verify.
5. **[HARD GATE D6] Cloud runs**: per-symbol `batch_manifest_v2_<sym>_hourset02c_{canary,scout}`
   mirroring the symbol's current B scout at HEAD (CL manifests adopt the `cl_` infix);
   **canary strictly before scout per symbol**; GCS preflight before every batch.

## Tests to add (TDD scope)
- `scripts/verify_02c_parity.py` with its own test module (shared-column byte-identity incl. a
  mutated-feature fixture that MUST fail; exact-31-list; index identity).
- `tests/test_curve_features.py` additions: grain-like session-gap fixture; quarterly-roll
  (ES/NQ) long BARS_SINCE_ROLL fixture. Existing 40 curve tests + feature_buckets stay green.
- NO `FeatureConfig` change unless the Phase-3 scan forces `curve_ffill_limit_bars` — then
  additive default + half-state validator + schema tests + canary (condition 5).

## Human decisions (operator)
| # | Decision | RESOLVED (operator, 2026-07-12/13) |
|---|---|---|
| D1 | Databento spend ≈ $10.65 | **AUTHORIZED**; estimate re-confirmed $10.65; 14 leg jobs submitted 2026-07-13 (submit waits + downloads; CL.c.0 landed first: GLBX-20260713-T5RBQMYKBQ) |
| D2 | Curve column set | **Full-31 for 02C** (condition 9). Follow-up ticket: **02D = pruned generation for ALL symbols**, each pruned from its own 02C feature-importance audit (NG audit methodology as template) |
| D3 | Target additions | **Per-symbol DIFF at build time** — add only missing short-horizon targets (verified: CL 15B lacks 1H/2H → gains 2x1_1H + 2x1_2H). **1x0.5_1H is added NOWHERE** (operator: unused/retired); symbols already carrying it keep the columns untouched (byte-identity), removal deferred to 02D |
| D4 | GC/SI second leg | decided from Phase-3 scan evidence (condition 8) — unchanged |
| D5 | Legacy NG 03B GCS object | **DELETE** — but ONLY AFTER `NG_HourSet_02C.parquet` is uploaded, `ls`-verified, and byte-equality vs 03B has passed. Local legacy parquet may also be removed then |
| D6 | VM spend | **PRE-AUTHORIZED**: canary→scout chains run automatically as each symbol passes its parity gates (canary strictly first). New stamp format `batch_<ts>_<SYM>_<DATASET>_<TIER>` implemented (run_sweep_batch.ps1 + resume_batch.ps1 + workflow doc) — first 02C canary validates it |
| D7 | Symbol set + control arms | **Keep all 8 symbols** (operator explicitly retains NQ/ZC/ZS to re-test under the new baseline methodology despite NQ≈ES overlap and old ZC/ZS no-edge results); NO control arms; curve-attribution caveat applies to NQ/ZC/ZS (condition 10) |
