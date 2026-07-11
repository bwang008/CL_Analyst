# Auditor RCA + Fix Proposal — jit-roll-ratio-empty_07102026_1453

Auditor: Ticket-Auditor | Date: 2026-07-10 | Status: PROPOSAL ONLY (no source files modified; fleet live-running from this checkout)

## 1. Root cause — CONFIRMED, with four amendments

### Component 1 (confirmed) — empty ratio ledger, no backfill mechanism
- `src/live_execution/data_manager.py:1104-1106` — `get_ratio_adjusted_df()` early-returns the RAW cache copy when `self._roll_ratios` is empty. Verified.
- `data_manager.py:357-367` — `_roll_ratios` is populated ONLY from `roll_history` in the metadata file at `initialize()`. Verified.
- All five files (`C:\CL_Analyst_Data\data\processed\.roll_metadata{,_ES,_NG,_GC,_SI}.json`) dump as `"roll_history": []`, `"cumulative_ratio": 1.0` (updated_at 2026-07-10T00:52–00:56 = last fleet restart). Verified by direct read.
- `_detect_rollover()` (`data_manager.py:965-993`) returns False on first run ("No previous front-month recorded"); nothing in the codebase can ever record ratios for the ~9 months of seams already embedded in `<SYM>_raw_1h.parquet` seeds. Verified.
- Net effect: inference (`live_trader.py:4325`) and warmup (`live_trader.py:3273`) run `build_live_features()` on the raw stitched series for all 5 symbols, while every deployed model trained on ratio-adjusted HourSets. Bug report evidence (NG 10/48 short flips, −31% Jan seam) accepted as-is; independently consistent with the code paths.

### Component 2 (confirmed) — mid-run rolls permanently unrecordable
- Ratio capture exists ONLY in `DataManager.initialize()` Step 2 (`data_manager.py:417-432`). `_save_roll_metadata()` is called ONLY from `initialize()` Step 5 (`data_manager.py:449`). Verified via grep: no other call sites.
- The live rollover handler `_check_contract_rollover()` (`live_trader.py:3517`, daily-gated poll) updates `data_manager_1h.front_month_id` in memory (`live_trader.py:3679-3683`) but never computes a ratio and never persists metadata.
- The 1h Brain stream is a ContFuture subscription (`live_trader.py:3488-3490, continuous=True`) and is torn down/re-subscribed after every reconnect (`live_trader.py:3778-3817`). When IBKR flips the CONTFUT lead, appended cache bars silently switch basis mid-run.
- At the next restart the front-month mismatch IS detected, but `_compute_roll_ratio()` (`data_manager.py:995-1050`) compares the last 50 overlap bars of a NOW-anchored "3 D" CONTFUT fetch against the cache tail — post-roll vs post-roll → ratio ≈ 1.0 → swallowed by the tolerance branch at `data_manager.py:425-432`. Seam permanently unrecorded. Confirmed exactly as reported.
- Deadline: next CL roll ~2026-07-20 (front month CLQ6).

### Amendment 1 (NEW latent defect, "Component 3") — `_apply_roll_to_cache` double-adjusts the overlap window
`data_manager.py:1052-1090`: cutoff is recorded as `roll_ts = self._df.index.max()` and THEN the last ~3 days of cache bars are overwritten with fresh new-basis IBKR bars. `get_ratio_adjusted_df()` multiplies every bar `< roll_ts` by the ratio — including those just-overwritten new-basis bars → they end up at `new_basis × ratio` (≈ old×r²). The adjusted series gets a spurious step at overlap-start and a second at the last bar: up to ~3% distortion across a 3-day window for CL-sized rolls. Never exercised in production (roll_history has always been empty everywhere), but it corrupts the FIRST real roll the moment component 2 is fixed. Correct semantics: cutoff must be the first new-basis bar (overlap start if overwriting; otherwise skip the overwrite).

### Amendment 2 — CL tolerance swallows real CL rolls
`src/core/instrument_master.py:81`: CL/MCL `roll_ratio_tolerance = 0.01` (legacy pin). Evidence shows real CL monthly roll gaps of 0.2–2.8% — any gap < 1% is silently tolerance-skipped even by a correctly witnessed roll (`data_manager.py:425`, `:876`). Sub-tolerance seams compound over months. All other symbols are already 0.001.

### Amendment 3 — HourSet÷raw cannot derive post-HourSet seams
The bug report's own evidence (item 4) notes CL training data ends 2026-06-12; CL's ~Jun-18 seam is NOT recoverable from HourSet÷raw. Remediation A needs a second ratio source for seams between HourSet-end and now:
- ES/NG/GC/SI (+NQ/ZC/ZS): `C:\CL_Analyst_Data\data\raw\DataBento\<SYM>\<SYM>_ratio.csv ÷ <SYM>_raw.csv` — Databento downloads dated Jul 1–4, 2026, cover the Jun seams. Must first sanity-gate that `<SYM>_ratio.csv` ≡ the HourSet basis on overlap.
- CL (no DataBento folder): one-time IBKR expired-contract overlap fetch (CLN6 `includeExpired=True` vs CLQ6, median same-timestamp Close ratio over ~Jun 15–19) — the same estimator `_compute_roll_ratio` uses, applied to the explicit contract pair.

### Amendment 4 — restore-filter ownership makes naive backfill fragile
`data_manager.py:357-367` skips any `roll_history` entry whose `"to"` does not `startswith(execution_symbol)`. Current execution symbols (from `last_front_month_by_symbol` legacy-key writers): CL→`CL`, ES→`MES`, GC→`MGC`, NG→`NG`, SI→`SIL`. Backfilled entries must be prefix-correct TODAY, and would be SILENTLY DROPPED (basis reverts to raw with no error) if an execution symbol is ever swapped (e.g., SIL→SI) — a no-silent-null-defaults violation waiting to happen. Fix: backfill entries carry `"origin": "seed_backfill"` and the restore loop bypasses the ownership filter for them (they are written once per FILE, not once per execution symbol, so unconditional restore is the correct semantics — the filter exists only to dedupe per-execution-symbol duplicates of live-witnessed rolls).

## 2. Severity: HIGH
Multi-file structural fix; live fleet trades all 5 symbols on the wrong price basis with measured signal flips (NG 10/48 shorts suppressed during a −12% move); latent component corrupts every future roll with a hard deadline (~2026-07-20 CL); remediation touches `data_manager.py`, `live_trader.py`, `instrument_master.py`, external metadata files, plus a migration script.

## 3. Recent regression? NO
- JIT per-symbol roll machinery landed 2026-07-05 (`f383662`, T5) with the gap present from day one; seeds staged 2026-07-04/05 already contained the seams.
- `git log -n 5` on both files shows no subsequent change to ratio semantics (latest touches: `b330abf` dtype coercion 07-08; live_trader recovery fixes 07-07/09).
- `roll_history` has been empty since file creation — the defect was never masked and never worked.

## 4. Proposed fix — A + C combined (reject B), in two independently-shippable stages

### Verdict on the three candidates
- **A (backfill migration): ACCEPT** — least invasive, zero live-code change strictly required, restores training basis for all embedded seams. Alone it is insufficient (dies at the next roll).
- **B (re-stage seeds on adjusted basis): REJECT** — breaks the split-brain invariant ("cache stays 100% RAW", `data_manager.py:1055`): execution/bracket-ATR reads the same cache, so SL/TP distances would be scaled by the cumulative factor (~10% error on NG); requires cache rebuilds losing live-accumulated bars; does NOT remove the need for C at the next roll; invalidates comments/tests pinned to the raw-cache invariant.
- **C (mid-run capture): ACCEPT with redesign** — capture must anchor on the DATA basis switch (CONTFUT quotient scan), not the fleet's own front-month poll, because IBKR's lead flip lags/leads `get_front_month_contract()` and the stream flips at resubscribe time. Must persist immediately and must fail LOUD when unresolvable.

### Stage 1 — Migration (Fix A, external data only; no source edits)
New one-time script `scripts/backfill_roll_history.py` (repo, committed; runs against `CL_DATA_ROOT`):
1. Per symbol, load the deployed model's training HourSet (from the live child's config `dataset` reference — do not hardcode), the seed/cache `*_raw_1h.parquet` + `warm_start_cache_<SYM>_1h.parquet`, and compute per-timestamp quotient `q_t = HourSet_close / raw_close` on the overlap. Segment it: within-segment CV must be < 1e-6 (evidence: 2e-8) — HARD FAIL otherwise (no silent tolerance).
2. Convert cumulative factors to per-roll ratios in the LIVE code's convention: with segments 0..K oldest→newest and factors f_j (f_K normalized to the newest overlap segment), the ratio stored for the seam between segment k−1 and k is `r_k = f_{k−1}/f_k`, cutoff = first bar timestamp of segment k (`get_ratio_adjusted_df` masks `index < roll_ts`). NOTE: the bug report's "per-roll ratio = next/prev cumulative" is the INVERSE of the live replay convention — direction must be established by the replay-equality gate below, never by formula trust.
3. Post-HourSet seams (Amendment 3): extend the quotient scan using `DataBento/<SYM>/<SYM>_ratio.csv ÷ <SYM>_raw.csv` (after gating ratio.csv ≡ HourSet basis on overlap); for CL, one-time IBKR expired-pair fetch (CLN6/CLQ6). Every seam between seed start and NOW must be covered or the script HARD FAILS for that symbol.
4. Write entries into `.roll_metadata_<SYM>.json` (timestamped backup copy first): `{"from": <pre-roll contract or "seed">, "to": <execution-symbol-prefixed contract label>, "ratio": r, "timestamp": <migration time>, "timestamp_cutoff": <seam iso>, "origin": "seed_backfill_jit-roll-ratio-empty_07102026_1453"}` — `"to"` prefixes: CL/MES/MGC/NG/SIL (Amendment 4; the `origin` key is inert to today's reader). Recompute `cumulative_ratio` as the product (informational only per `data_manager.py:355-356`).
5. **Validation gate (mandatory, in-script):** offline replay — construct `DataManager(symbol=…, data_client=None)` against COPIES of cache+metadata in scratch, call `initialize()` + `get_ratio_adjusted_df()`, and assert (a) `adjusted_close / HourSet_close` is CONSTANT over every overlap timestamp (max rel. deviation < 1e-6) and equals the product of post-HourSet per-roll ratios; (b) the final cache bar is byte-equal to raw; (c) feature-level spot check — `build_live_features()` on the adjusted frame reproduces the HourSet's stored feature columns (at minimum `TS_VOL_YZ_ZSCORE_72v840` for NG) on overlap timestamps within float tolerance.
6. Activation = per-child restart (ratios restore only at `initialize()`); running children are unaffected until then (`_save_roll_metadata` preserves existing `roll_history` on rewrite, `data_manager.py:870-871`, and only runs at startup — no mid-run write race).

### Stage 2 — Live-code fix (Fix C + Amendments 1/2/4; requires canary per user rule)
Target files: `src/live_execution/data_manager.py`, `src/live_execution/live_trader.py`, `src/core/instrument_master.py`.
1. **`data_manager.py` — new `resolve_roll_seam()`** (used by mid-run capture AND restart pending-roll resolution): fetch CONTFUT history (duration sized to cover `detected_at − 2d` → now, default "5 D"); compute per-bar `q_t = ibkr_close/cache_close` over the overlap; classify bars old-basis (`|q−1| > tolerance`) vs new-basis; outcomes:
   - all old-basis → IBKR lead not flipped yet → RETRY later (recording now would leave subsequently-appended old-basis bars unadjusted);
   - all new-basis (≈1) → nothing to anchor on → escalate (see pending-roll hard fail);
   - old-run then new-run → `ratio = median(q_t of old-basis run)`, `cutoff = first new-basis timestamp`; append to `_roll_ratios/_roll_timestamps` via a new shared `_append_roll_event(from, to, ratio, cutoff)` helper and PERSIST metadata immediately (refactor the append block out of `_save_roll_metadata:874-892` so mid-run persistence doesn't depend on `initialize()`).
2. **`live_trader.py` `_check_contract_rollover`** (after step 4, ~3683): write a `pending_roll` record `{from, to, detected_at}` into roll metadata (new namespaced key), then attempt `resolve_roll_seam()` on `data_manager_1h` (and `_5m` when present); on RETRY, re-attempt from the 5-minute poll cycle each new 1h bar; if unresolved after a deadline (e.g., 3 days), log.critical + Telegram — never a silent skip. Clear `pending_roll` on success.
3. **`data_manager.initialize()`**: if metadata carries an unresolved `pending_roll` for this execution symbol, resolve via the same seam scan with a widened fetch window; if unresolvable (fleet down too long, IBKR window exhausted) → HARD FAIL with operator remedy text (no-silent-null-defaults) instead of today's `ratio≈1 → "within tolerance"` swallow at `:425-432`.
4. **Amendment 1 fix**: in `_apply_roll_to_cache`, record the cutoff as the FIRST overwritten overlap timestamp (or stop overwriting and keep cutoff at max — pick one basis-consistent convention); add a regression test that replays a synthetic roll and asserts the adjusted series has exactly one seam step.
5. **Amendment 4 fix**: restore loop (`:357-367`) unconditionally restores entries with `origin == "seed_backfill"`.
6. **Amendment 2 fix** (separate reviewed line-item): `instrument_master.py:81/:100` CL/MCL `roll_ratio_tolerance` 0.01 → 0.001. Safe because ratio capture is keyed on detected front-month change / basis-switch scan, not free-running; the median-of-50 same-timestamp quotient noise floor is ≪ 0.1%.
7. Tests: unit tests for the seam scan (pre-flip retry, post-flip seam location, mixed-tail median robustness with ≤25 contaminated bars), pending-roll restart resolution, hard-fail path, and the Amendment-1 single-seam invariant.

### Validation / rollout / rollback
- Order: land Stage 1 first (data-only, no code); NG child restart as the live canary (largest measured distortion; compare live `shadow_log` probs in `fleet_telemetry.db` against the reporter's training-basis reconstruction for the first sessions). Then fleet-wide coordinated restart. Stage 2 lands as a normal code change: canary run REQUIRED before scout/prod (user rule), and NO working-tree edits while children run — schedule with a fleet restart window.
- Stage 2 must be live before ~2026-07-20 (CL roll). If Stage 2 slips, contingency: at the CL roll, take the fleet down BEFORE IBKR's lead flip and restart after — the EXISTING startup path then witnesses the roll legitimately (with Amendment-1 fixed, or accepting its 3-day distortion if not).
- Rollback: Stage 1 — restore timestamped metadata backups + restart children (raw basis, today's behavior). Stage 2 — revert commit; metadata written by the new path remains valid for the old reader (same schema).

## 5. Risks / side effects
1. **Intended behavior change**: restored basis will shift live signals (that is the point) — NG shorts re-enable in the current regime; position sizing/threshold behavior must be watched during canary.
2. **HourSet-choice mismatch**: if the migration derives ratios from a different HourSet than a model trained on, per-segment quotients could differ (different roll schedules). Mitigated by deriving from each child's configured dataset + the CV gate; symbols sharing one metadata file but different training sets would need reconciliation (not the case today — one 1h child per symbol).
3. **Ratio-direction inversion** is the single most dangerous migration bug (would ~square the error). The replay-equality gate (5a-c) makes it impossible to ship inverted.
4. **Post-HourSet seam sources**: Databento ratio.csv basis must be proven identical to HourSet basis before use; CL's IBKR expired-contract fetch depends on IBKR still serving CLN6 hourly history (it does within ~2 years, but verify before relying on it).
5. **Execution-symbol swaps** silently orphan backfilled entries until Amendment 4 lands — document in the migration output and fleet preflight.
6. **5m managers** restore the same entries (shared file, same execution symbol): cutoffs predating the 5m cache multiply zero bars (harmless); cutoffs inside its range adjust it correctly. No fleet child currently consumes 5m `get_ratio_adjusted_df()` for inference (all 1h), but verify before Stage 2 ships.
7. **Mid-gap restarts between detection and resolution**: covered by the persisted `pending_roll` + widened fetch; if the fleet is down beyond IBKR's fetchable window, the HARD FAIL stops trading loudly — an availability cost accepted deliberately over silent wrong-basis trading.
8. Out of scope, flagged: GC/ES always-above-threshold losses are a model-selection issue (bug report evidence 4), NOT fixed by this ticket.
