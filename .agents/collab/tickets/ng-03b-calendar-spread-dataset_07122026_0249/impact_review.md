# Impact Review — ng-03b-calendar-spread-dataset_07122026_0249

**Role:** Ticket-Impact-Reviewer (performed inline by Ticket-Manager; native subagent protocol unavailable)
**Proposal reviewed:** `auditor_proposal.md` (same ticket folder)
**Decision:** **APPROVE WITH CONDITIONS** (8 conditions, folded into the blueprint) + 5 items requiring HUMAN decision before/при TDD hand-off.

## Constraint rules check
- **Interface Rule:** PASS — no function signature changes. `FeatureConfig` gains 3 additive fields
  with explicit defaults; every existing DataMap/manifest parses identically (pydantic ignores the
  absence; defaults are documented values, not silent nulls).
- **Base Class Rule:** TRIGGERED-but-contained — `data_processor.py` and `schemas.py` are core
  shared modules. Justification accepted: the edit is a single gated step (default-off flag) and the
  proposal carries a mandatory byte-parity control-regression gate (rebuild 02B, assert equality).
  Localized alternatives were genuinely considered (see "Simpler alternative" below).
- **Refactor Veto (multi-component rewrite):** NOT triggered — one new module + three localized,
  additive edits. No mandatory human-authorization halt on this ground. Human gates listed below are
  spend/risk decisions, not refactor authorization.

## Blast-radius map
| Touchpoint | Radius | Assessment |
|---|---|---|
| `src/features/curve_features.py` (new) | none (new file, imported only behind the flag) | OK |
| `schemas.py::FeatureConfig` | every DataMap validation, cloud-zipped code | Additive+defaults = zero behavior change for existing configs. NOTE: the 03B **scout manifest must NOT carry the curve fields** — the VM never rebuilds features; keeping the manifest a pure mirror of 02B (dataset_version only) also removes any old-zipped-code schema-drift concern. |
| `data_processor.py::process_from_config` | ALL symbols' dataset builds | Guarded by flag + Condition 1 (02B parity rebuild). |
| `feature_buckets.py` | `--use-buckets` production path for ALL symbols | Adding "curve" adds one toggle dim (2^11→2^12) even for symbols with no CURVE_ columns (a no-op toggle, but a spurious Optuna dim in bucket mode). Acceptable; update the `BUCKET_MIN_TRIALS` comment. The v2 scout A/B itself never toggles buckets (verified `vm_e2e_pipeline.py`) — registration is hygiene, not an A/B dependency. |
| New configs (DataMap + 2 manifests) | none until launched | OK; dry-run gates arbitrate. |

## Genuine risks found (beyond the proposal)
1. **CONTROL-CLOBBER HAZARD (new — must fix in blueprint).** The proposal's control-regression gate
   says "rebuild 02B with the new code and assert equality". Done naively, `regenerate_features.py`
   would OVERWRITE the production `NG_HourSet_02B.parquet` in place — if the new code were buggy,
   the gate would destroy the control it is comparing against. **Condition: rebuild the 02B control
   to a scratch output** (temporary DataMap copy with `output_dir`/`output_filename` redirected to
   the scratchpad) and compare against the untouched original. Never point the parity rebuild at the
   real file.
2. **Non-paired trials (A/B honesty).** Adding 6 columns changes LightGBM's `feature_fraction`
   sampling stream even at seed 42 — 03B trials are NOT paired with 02B trials. The comparison is
   distribution-level (per-target holdout AUC / solo PnL), not trial-paired. This is inherent to
   every dataset A/B in this repo (14B→15B, 01B→02B) — acceptable, but the report must not claim
   trial-level pairing.
3. **Early-history second-leg gaps.** 2010–2012 NG.c.1 may have >24-bar gaps during traded front
   hours, which would hard-fail the build by design. Rather than guessing the ffill limit, run a
   cheap coverage scan on the downloaded legs BEFORE freezing `FFILL_LIMIT_BARS` in TDD
   (Condition 5). The proposed 24 stays the default hypothesis; the scan supplies the evidence for
   the human to confirm or adjust.
4. **Roll-jump contamination** of Z/WOW windows (~1–1.5 weeks per monthly roll on the 168 window)
   is real and NOT mitigated in v1. I concur with shipping v1 without masking/TTE-normalization
   (sign/Z/WOW already avoid the worst sawtooth carrier — the raw level — and tree models tolerate
   episodic artifacts), but this is an accepted-risk item the HUMAN must sign off, with the
   `CURVE_BARS_SINCE_ROLL` 7th-feature option explicitly offered (cheap, directly addresses the
   artifact, slightly dilutes the clean 6-feature story). My recommendation: defer it; revisit in
   03C only if FI shows CURVE_ features rank but holdout doesn't improve.
5. **Manifest tier drift.** The 01B canary template predates the 12-mo-holdout and other box
   changes. Mirror it verbatim (per the never-cross-tiers rule) EXCEPT dataset_version/names, and
   let `-DryRun` gates arbitrate holdout/block-layout validity at HEAD; if dry-run rejects the old
   box, align ONLY the rejected fields to the 02B scout values and note it in `_comment`.
6. **Lineage side-effect.** `regenerate_features.py` writes a lineage JSON under the repo
   (`data/processed/NG_HourSet_03B_config.json`) — fine, but it means even the "build" step touches
   the working tree: schedule ALL of this after the in-flight batch completes (also required by the
   canary-before-pipeline-change rule and the never-edit-tree-mid-batch rule).

## Leakage review (independent pass)
- Concurrent tz-naive join, no publication-lag shift: CORRECT for market data (COT's +3BD shift is
  about report publication, not applicable here). Verified both sides share the Databento
  `ts_event`(interval-start, UTC)→naive convention, so there is no relative lookahead.
- Inner-join before feature computation prevents stale-leg/fresh-leg mixing. ffill-only afterwards
  (bounded); no bfill anywhere. Rolling z / shift(168) are strictly backward-looking.
- REQUIRED TEST (Condition 6): mutate leg rows strictly after time T → assert all CURVE_ values at
  ≤ T are byte-identical. This is the one test that proves no-lookahead structurally.
- Sign feature at exactly F2==F1 emits 0 — fine, explicit.
- Zero-std z := 0.0 — explicit and documented; acceptable (never inf/silent NaN).

## Simpler alternative seriously weighed
**Post-process the existing 02B parquet** (load, append 6 CURVE_ columns, save as 03B) — zero
changes to shared pipeline code, near-zero blast radius. REJECTED, with reservations noted:
it breaks the DataMap-as-single-source-of-truth lineage (03B would be unreproducible end-to-end,
`*_config.json` lineage would lie), and the engine+flag work is needed anyway the moment 03B wins
and must be regenerated/extended. The default-off flag + parity gate reduce the pipeline-change risk
to approximately the post-processing level while keeping lineage honest. If the HUMAN wants the
absolute-minimum-risk probe first, the post-processing variant is the fallback — but it must then be
labeled a THROWAWAY probe, never promoted.

Also re-affirmed the Auditor's rejections: v.0-front reuse (rank-flip noise), Databento native
spread instruments (unverified symbology), AlphaFactory embedding (single-series architecture),
TTE normalization in v1 (new expiry-calendar dependency for marginal v1 benefit — Impact-Reviewer
verdict as requested by the ticket: NOT worth the complexity now).

## Conditions of approval (binding on the blueprint)
1. Control-regression gate rebuilds 02B to a SCRATCH path; the production 02B parquet is read-only
   throughout (fixes Risk 1).
2. A/B parity gate (index equality + TARGET_ equality + exactly-6-new-columns + zero-NaN) is a
   hard gate before upload; failure = do not upload, do not launch.
3. The 03B scout manifest is a pure mirror of the 02B scout @ HEAD (dataset_version, labels,
   gcs_prefix, _comment only); it does NOT carry curve schema fields.
4. Evaluation is model-level primary (per-target holdout AUC + solo holdout PnL + FI ranks of
   CURVE_*); ensemble PnL secondary with the pair-selection-collapse caveat stated in the report.
5. `FFILL_LIMIT_BARS` default 24 is provisional: a leg-coverage scan (scratchpad, after download)
   must be run and its result put in front of the human before the constant is frozen in TDD.
6. No-lookahead mutation test + gap-raise test + leading-NaN-budget test are mandatory in
   `tests/test_curve_features.py`.
7. All repo edits and builds happen AFTER the in-flight batch completes; canary before scout
   (pipeline code changed); dry-run gates green before any VM.
8. `include_curve_spread=True` with missing leg paths must raise; leg paths with flag False must
   raise (config hygiene, no half-states).

## Human decisions required (blocking)
- H1: Databento spend authorization for NG.c.0 + NG.c.1 full-history pull (estimate first; expected
  small at ohlcv-1h, but the rule is estimate → report → authorize).
- H2: Accept the v1 roll-jump artifact (no masking/TTE); decide on the optional
  `CURVE_BARS_SINCE_ROLL` 7th feature (reviewer recommends: defer).
- H3: Confirm `FFILL_LIMIT_BARS` after the coverage scan (default 24).
- H4: Canary + scout VM spend authorization (canary mandatory before scout).
- H5: Scheduling — everything waits for the in-flight batch to complete.

---

# v2 ADDENDUM — Impact Review of the HUMAN revision plan (full angle-coverage + seasonality)
**Date:** 2026-07-12. **Scope reviewed:** blueprint v2 (superset CURVE_* + Time_Month_* +
SEASONAL_Z) with the coordinator's BINDING corrections R1–R8. **Decision:** v1 approval STANDS,
extended to v2 WITH the additional conditions below. All v1 conditions 1–8 remain binding;
v1 human decision H2's "defer BARS_SINCE_ROLL" clause is REVOKED by the human plan (now in scope).

## Binding corrections R1–R8 — recorded with independent verification
- **R1 (conflict resolved):** `add_term_structure_shapes` hard-codes the `TS_` output prefix —
  CONFIRMED (`alpha_factory.py:868, 873, 882, 888, 898, 905, 910`). Routing spread_pct through it
  would land curve outputs in the `term_structure` bucket and violate the plan's own CURVE_/TS_
  separation; it would also add columns to every dataset that calls the generator, threatening the
  §7.1 parity gate. Mirror the shape logic locally with CURVE_ naming; `alpha_factory.py`
  untouched. ACCEPTED.
- **R2 (conflict resolved):** unconditional `Time_Month_*` in the shared cyclical-time step would
  break the rebuild-02B byte-parity control gate. Gating behind `include_month_encoding=False`
  (03B: True) preserves the gate; the bucket-prefix fix is training-side only (no dataset bytes).
  ACCEPTED — this is the same reasoning as v1's default-off `include_curve_spread`.
- **R3:** signed-series rule CONFIRMED in-house (`alpha_factory.py:765-767`: signed indicators get
  Diff/Sign_Agreement/Regime_Cross, "no Ratio — division on zero-crossing data creates asymptotic
  instability"). ROC as simple `.diff(n)`, no pct_change, no Ratio/Log_Ratio/Invert on spread_pct.
  The plan's claim that Ratio/Log_Ratio family outputs "cover the ratio angle automatically" is
  WRONG for a signed series; ratio angle covered by VOLRATIO (positive vol series — legal) +
  Z_DIFF. ACCEPTED.
- **R4:** house windows CONFIRMED (02B DataMap windows 24/72/168/336/840; macro horizons
  2160/4320). Z set = 24/72/168/336/840/2160; MOM=shift(840); PCTL_840; *_24v840. ACCEPTED.
- **R5:** percentile MUST be native `.rolling(w).rank(pct=True)` — CONFIRMED pattern at
  `macro_features.py:616` (the post-c1c78fc fast path; the naive apply-rank was the 339x
  MACRO_PCTILE hotspot). ACCEPTED.
- **R6:** warmup reasoning CONFIRMED: curve max window 2160 < existing 4320-bar macro max, so the
  curve leading-NaN region (~2160 joined bars from the 2010-06 leg start) ends well before the 02B
  parquet's first surviving row (post-4320-bar warmup). 0.0 neutral fill restricted to seasonal
  cold-start + zero-std; all else fail-loud. ACCEPTED.
- **R7:** the time-bucket fix + Time_Month columns make the in-flight 02B scout non-code-identical
  to the 03B arm → new blocking decision H-ab-code-parity; reviewer CONCURS with option (a)
  (rerun the 02B arm at the same HEAD, +1 scout cost) — attribution is this program's recurring
  weak point (see NG 02B pair-selection collapse); paying one scout for a clean control is cheap
  relative to a mis-attributed conclusion. ACCEPTED as blocking human decision.
- **R8:** week-53 handled by the distinct-prior-years gate (merge-into-52 surfaced as an option);
  CURVE_ROLL_YIELD deferral verified — NO historical days-to-expiry series exists training-side
  (grep of `src/core`/`src/live_execution`: only live roll timing / `roll_buffer_days`); "bars to
  next observed roll" correctly identified as lookahead and BANNED as a substitute;
  BARS_SINCE_ROLL feasible via existing `is_roll` detection (`parse_raw_csv`;
  `scripts/backfill_roll_history.py` precedent). ACCEPTED.

## New risks introduced by v2 (beyond v1)
1. **SEASONAL_Z is the highest-leakage-risk column in the set.** Grouped expanding statistics are
   exactly where subtle lookahead hides (group-then-sort bugs, shift applied before grouping,
   pandas groupby order instability). MITIGATION (new Condition 9): the expanding+shift(1)
   construction must be per-group in strict time order; the causality fixture (future-year
   mutation invariance) and the all-columns no-lookahead mutation test are MANDATORY and must
   cover SEASONAL_Z (and SEASONAL_PCTL if opted in).
2. **Superset size (29+2 vs 6).** Materially raises the multiple-comparisons surface: with ~31 new
   columns, SOME will rank in FI by chance. MITIGATION: evaluation stays model-level
   (AUC/solo-PnL) — FI ranks are diagnostic, never the promotion criterion; the superset
   philosophy matches the house `add_term_structure_shapes` precedent, and the curve bucket toggle
   gives bucket-mode runs a clean off-switch. Residual risk accepted per the human's explicit
   philosophy choice.
3. **Slope/R2 and window semantics on the JOINED timeline.** Windows count joined bars, which can
   deviate from bar-index bars where NG.c.1 gaps exist; `_rolling_slope_r2_numba` NaN-skips are
   moot post-inner-join. Document in the docstring; acceptable (Condition 10: docstring must state
   the joined-timeline window semantics).
4. **2160-window vs 2200-row warmup margin** is only ~40 bars on paper, but the binding constraint
   is the 02B index (post-4320 warmup), not the 2200 constant — the §7.3 index-parity gate is the
   hard arbiter either way. No change needed.
5. **Time-bucket fix ripples into existing tests** (`test_feature_buckets.py:48-49` assert the
   fictional `Hour_sin`/`DayOfWeek_cos` prefixes). Rewriting them is REQUIRED — they currently
   encode the bug as expected behavior.
6. **Feature-count bloat for LGBM** (~31 added to a few-hundred-column matrix): negligible;
   `feature_fraction` 0.4–0.9 and tree regularization absorb it. No optuna-box change — KEEP the
   identical box for A/B cleanness.

## Could NOT accommodate from the human plan (with reasons)
- `CURVE_ROLL_YIELD` in 03B by default — no legal days-to-expiry source exists training-side;
  deferred to 03C with a loud log unless H-rollyield commissions an expiry calendar (plan itself
  allowed this branch).
- Direct reuse of `add_term_structure_shapes` — R1 (TS_ prefix hardcode).
- pct_change ROC + "ratio angle via Ratio/Log_Ratio families" — R3 (signed series).
- ~720-bar "1M" window — R4 (house 840/2160 conventions).

## Additional conditions of approval (v2; cumulative with v1's 1–8)
9. SEASONAL_Z (and optional PCTL) ship only with the causality fixture + all-columns no-lookahead
   mutation test green; cold-start emits 0.0 (never NaN, never row drops); distinct-prior-YEARS
   counting proven by test.
10. `curve_features.py` docstring states: CURVE_/TS_ separation; NG.c.2-curvature out of scope;
   joined-timeline window semantics; the two-and-only-two 0.0-neutral exceptions.
11. The §7.3 parity gate asserts the LITERAL new-column name list (from DataMap lineage +
   resolved human decisions), not a bare count.
12. H-ab-code-parity is BLOCKING for the scout launch (not for TDD): the human must pick control
   arm (a) or (b) before any 03B-vs-02B conclusion is drawn.
