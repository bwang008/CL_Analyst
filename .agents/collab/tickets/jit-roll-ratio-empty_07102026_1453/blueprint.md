# Ticket Resolution Blueprint — jit-roll-ratio-empty_07102026_1453
**Ticket Directory:** `.agents/collab/tickets/jit-roll-ratio-empty_07102026_1453/`

**Status:** HUMAN AUTHORIZED 2026-07-10 (both stages; Impact-Reviewer escalated under the multi-component-refactor guardrail, human explicitly authorized Stage 1 and Stage 2 scope). Supporting docs in this folder: `bug_report.md` (evidence), `auditor_rca_and_fix_proposal.md` (full RCA), `impact_review.md` (verification + conditions).

## Bug Summary

Live fleet inference (CL/ES/NG/GC/SI, all 1h models) runs on the RAW stitched futures series while every deployed model trained on RATIO-ADJUSTED HourSet data. `DataManager.get_ratio_adjusted_df()` (src/live_execution/data_manager.py:1104-1106) replays ratios from `roll_history` in `C:\CL_Analyst_Data\data\processed\.roll_metadata_<SYM>.json` — and all five files have `roll_history: []`, so the "adjustment" is a no-op. Two causes: (1) the 1h seeds embed ~9 months of roll seams that no mechanism ever recorded ratios for; (2) ratio capture exists only in `initialize()`, so a mid-run IBKR CONTFUT basis flip is compared post-roll-vs-post-roll at the next restart → ratio≈1 → silently tolerance-swallowed (data_manager.py:425-432) → seam permanently unrecorded. Measured impact: NG 10/48 short-signal flips during a −12% move (seams up to −31%). Latent third defect (Amendment 1): `_apply_roll_to_cache` (data_manager.py:1052-1090) records cutoff = `index.max()` BEFORE overwriting the ~3-day overlap with new-basis bars → JIT replay double-adjusts that window at the first genuinely-witnessed roll. Also: CL/MCL `roll_ratio_tolerance = 0.01` (instrument_master.py:81) exceeds real CL roll gaps (0.2–0.9%), defeating capture for CL even when witnessed. Deadline driver: next CL roll ~2026-07-20.

## Target Files

Stage 1 (data-only migration):
- `scripts/backfill_roll_history.py` (NEW)
- `C:\CL_Analyst_Data\data\processed\.roll_metadata{,_ES,_NG,_GC,_SI}.json` (data writes, timestamped backups first — not git-tracked)
- `tests/` — new tests for the migration's derivation + replay-validation helpers (structure the script so derivation/validation are importable functions)

Stage 2 (live-code fix):
- `src/live_execution/data_manager.py`
- `src/live_execution/live_trader.py`
- `src/core/instrument_master.py`
- `tests/test_session_watchdog_rollover.py` (pinned tolerance assertions at :844 and :859 MUST be updated — this deliberately reverses the T5 "zero-change pin")
- New/updated tests: seam-scan unit tests, pending-roll lifecycle, hard-fail paths, single-seam invariant; review behavioral assertions in `tests/test_data_manager_ratio.py`, `tests/test_rollover.py`, `tests/test_data_manager.py`, `tests/test_feature_parity.py`

## Required Changes

### Stage 1 — `scripts/backfill_roll_history.py` (ship first; independently valuable)

1. Per symbol {CL, ES, NG, GC, SI}: load (a) the deployed child's training HourSet — resolve from the live strategy config's dataset reference, do NOT hardcode filenames; (b) the live 1h cache and seed under `CL_DATA_ROOT/data/processed/`. Compute per-timestamp quotient `q_t = HourSet_close / raw_close` on the timestamp intersection and segment it into piecewise-constant runs. HARD FAIL the symbol if any within-segment coefficient of variation ≥ 1e-6 (observed real value ≈ 2e-8) — no tolerance widening, per the no-silent-null-defaults rule.
2. Convert cumulative segment factors to per-roll ratios in the live replay convention of `get_ratio_adjusted_df()` (multiplies bars `index < timestamp_cutoff` by `ratio`): for segments ordered oldest→newest with factors f_0..f_K, the entry for the seam between segments k−1 and k is `ratio = f_{k−1}/f_k` with `timestamp_cutoff` = first bar timestamp of segment k. CRITICAL: ratio direction must be PROVEN by the validation gate (step 5), never assumed from the formula.
3. Post-HourSet seams (between HourSet end and now) must also be covered: for ES/NG/GC/SI derive from `CL_DATA_ROOT/data/raw/DataBento/<SYM>/<SYM>_ratio.csv ÷ <SYM>_raw.csv` — but FIRST gate that the ratio.csv basis is identical to the HourSet basis on their overlap (same segmented-quotient test); for CL (no Databento folder) perform a one-time IBKR expired-contract overlap estimate: fetch CLN6 (`includeExpired=True`) and CLQ6 hourly bars over ~2026-06-15..19 and take the median same-timestamp Close ratio. Every seam between seed start and NOW must be covered for a symbol or the script HARD FAILS for that symbol (partial coverage = silent basis break mid-window).
4. Entry schema (Reviewer condition 3.2.1 — swap-proof zero-code activation): write entries WITHOUT a `"to"` key so the existing legacy restore branch (data_manager.py:360, entries lacking a string `"to"` are restored unconditionally) applies regardless of execution symbol. Carry the contract label under `"to_contract"` and stamp `"origin": "seed_backfill_jit-roll-ratio-empty_07102026_1453"`, plus `"from"`, `"ratio"` (full precision, do not round to 6dp if avoidable; if reusing the writer that rounds, prove the validation gate still passes), `"timestamp"` (migration run time) and `"timestamp_cutoff"` (seam ISO). Before writing each metadata file, copy it to a timestamped backup alongside. Recompute `cumulative_ratio` as the product (informational only). Preserve all existing keys (`last_front_month*`, etc.) untouched.
5. MANDATORY in-script validation gate (load-bearing; HARD FAIL, abort before any real write if any check fails — validate against scratch copies first, then write):
   a. Offline replay: construct `DataManager(symbol=…, data_client=None)` against scratch copies of cache+metadata, run `initialize()` + `get_ratio_adjusted_df()`; assert `adjusted_close / HourSet_close` is constant across ALL overlap timestamps (max relative deviation < 1e-6) and equals the product of post-HourSet per-roll ratios.
   b. Assert the final (newest) cache bar is unchanged vs raw (adjustment touches history only).
   c. Feature-level reproduction: `build_live_features()` on the adjusted frame must reproduce the HourSet's stored feature columns on overlap timestamps within float32 tolerance — at minimum `TS_VOL_YZ_ZSCORE_72v840` for NG.
6. Idempotency: re-running the script must detect existing `origin`-stamped entries and either no-op or refuse loudly — never duplicate entries.
7. Activation/rollout (operator steps, document in script output): ratios restore only at `initialize()` → restart the NG child FIRST as live canary; record the shadow_log-vs-training-basis probability comparison into this ticket folder BEFORE the fleet-wide restart (Reviewer condition 3.2.4). Rollback = restore the timestamped metadata backups + restart.

### Stage 2 — live-code fix (canary-gated; land before ~2026-07-20)

1. `data_manager.py` — new method `resolve_roll_seam()`, shared by mid-run capture and restart resolution: fetch CONTFUT history covering `detected_at − 2 days → now` (default "5 D"); compute per-bar same-timestamp quotient `ibkr_close / cache_close` over the overlap; classify bars old-basis (`|q−1| > roll_ratio_tolerance`) vs new-basis. Outcomes:
   - ALL old-basis → IBKR lead has not flipped yet → return RETRY (recording now would strand later old-basis appends unadjusted);
   - ALL new-basis → nothing to anchor on → return ESCALATE (see item 3; this path is load-bearing per the Reviewer — it must be actionable, not just a log line);
   - old-run followed by new-run → `ratio = median(q over the old-basis run)`, `cutoff = first new-basis bar timestamp`; append via a new `_append_roll_event(from_, to, ratio, cutoff)` helper and PERSIST metadata immediately. Refactor the roll-event append/persist logic out of `_save_roll_metadata` (data_manager.py:874-892) so persistence no longer requires `initialize()`.
2. `live_trader.py` `_check_contract_rollover` (after the front_month_id updates at ~:3679-3683): persist a `pending_roll` record `{from, to, detected_at}` under a new namespaced metadata key, then attempt `resolve_roll_seam()` on `data_manager_1h` (and the 5m manager when present). On RETRY, re-attempt on each new 1h bar. If unresolved past a deadline (~3 days): `log.critical` + Telegram alert with explicit operator remedy — never a silent skip. Clear `pending_roll` on success.
3. `data_manager.initialize()`: when metadata carries an unresolved `pending_roll` for this execution symbol, resolve via the same seam scan with a widened fetch window; if genuinely unresolvable (fleet down beyond IBKR's fetchable window) → HARD FAIL with operator remedy text, REPLACING today's silent `ratio≈1 → within tolerance` swallow at :425-432. (Accepted availability cost: loud stop over silent wrong-basis trading.)
4. Amendment 1 fix in `_apply_roll_to_cache`: make cutoff and overlap-overwrite basis-consistent — record `timestamp_cutoff` = the FIRST overwritten (new-basis) overlap timestamp, or drop the overwrite entirely; pick ONE convention and add a regression test replaying a synthetic roll asserting the adjusted series contains EXACTLY ONE seam step.
5. Amendment 2 fix in `instrument_master.py` (:81/:100): CL and MCL `roll_ratio_tolerance` 0.01 → 0.001. REQUIRED companion: update the pinned assertions in `tests/test_session_watchdog_rollover.py:844/:859` and note in the commit message that this reverses the T5 zero-change pin (Reviewer condition 3.2.2).
6. Restore-loop safety test (replaces the originally-proposed Amendment-4 code change, which Stage 1's no-`"to"` schema makes unnecessary): add a unit test pinning that backfill entries (no `"to"` key, `origin` stamped) are restored by `initialize()` under EVERY current execution symbol (CL, MES, MGC, NG, SIL) and under a hypothetical swapped symbol.
7. Tests (TDD): seam-scan state machine (pre-flip retry, post-flip seam location, median robustness with ≤25 contaminated tail bars, all-new-basis escalation), pending_roll persistence across restart, initialize hard-fail path, Amendment-1 single-seam invariant, metadata schema back-compat (old reader ignores new keys).

### Cross-cutting constraints (both stages)
- The live fleet runs from this checkout: code edits do not affect running children, but ANY activation (metadata writes are safe; restarts are the activation) must be coordinated with the operator. Confirm no cloud-batch deploy is in flight before committing (code is zipped at optimizer-deploy time).
- No-silent-null-defaults: every new config/metadata field read must raise when missing; all validation gates HARD FAIL (no tolerance widening to "make it pass").
- Canary rule: Stage 2 requires a canary run before production use. Deadline pressure from the ~2026-07-20 CL roll must NOT skip the canary; the sanctioned pressure-relief valve is the contingency: schedule fleet downtime across IBKR's CL lead flip so the existing startup path witnesses the roll (with Amendment 1 fixed first).
- Rollback: Stage 1 = restore metadata backups + restart. Stage 2 = revert commit (metadata written by the new path stays valid for the old reader).
