# Review Packet — jit-roll-ratio-empty_07102026_1453
*(Prepared by Ticket-Manager for the Ticket-Impact-Reviewer. Contains the Auditor's confirmed root cause, proposed fix, and risk register. Severity/priority classifications are deliberately withheld — form your own unbiased impact opinion.)*

## Bug context

Live fleet models (CL/ES/NG/GC/SI, all `bar_size: "1h"`, running from this checkout against `CL_DATA_ROOT=C:\CL_Analyst_Data`) were found on 2026-07-10 to compute inference features on the RAW stitched futures series while every deployed model was trained on RATIO-ADJUSTED HourSet data. Verified effects: every live/training return mismatch lands exactly on contract-roll dates (NG 2026-01-25 seam −31%, Apr +6.4%, May +4.2%; CL 0.2–2.8% monthly; ES/GC/SI ~1%); re-running the deployed model pickles on both bases over the last 48 bars showed NG with 10/48 short-signal flips + 3/48 long flips (raw basis suppressed shorts during a −12% NG move), SI 1/48, CL/ES/GC 0/48 currently. Offline raw-basis reconstruction matched live `shadow_log` probabilities to ~0.001–0.03, confirming production inference basis == raw. This is NOT a recent regression: the JIT roll machinery landed 2026-07-05 (`f383662`) with the gap present from day one; `roll_history` has been empty since the metadata files were created.

## Auditor's confirmed root cause (with four amendments)

### Component 1 — empty ratio ledger, no backfill mechanism
- `src/live_execution/data_manager.py:1104-1106` — `get_ratio_adjusted_df()` early-returns the RAW cache copy when `self._roll_ratios` is empty.
- `data_manager.py:357-367` — `_roll_ratios` is populated ONLY from `roll_history` in `.roll_metadata_<SYM>.json` at `initialize()`.
- All five metadata files dump as `"roll_history": []`, `"cumulative_ratio": 1.0`.
- `_detect_rollover()` (`data_manager.py:965-993`) returns False on first run; nothing in the codebase can record ratios for the ~9 months of seams already embedded in the `<SYM>_raw_1h.parquet` seeds.
- Net effect: inference (`live_trader.py:4325`) and warmup (`live_trader.py:3273`) build features on the raw basis for all 5 symbols.

### Component 2 — mid-run rolls permanently unrecordable
- Ratio capture exists ONLY in `DataManager.initialize()` Step 2 (`data_manager.py:417-432`); `_save_roll_metadata()` is called only from `initialize()` Step 5 (`:449`). Grep-verified sole call sites.
- The live rollover handler (`live_trader.py:3679-3683`) updates `front_month_id` in memory only; never computes a ratio, never persists.
- The 1h Brain stream is CONTFUT (`live_trader.py:3488-3490`), re-subscribed on every reconnect; when IBKR flips the lead, cache bars silently switch basis mid-run.
- At the next restart, `_compute_roll_ratio()` (`data_manager.py:995-1050`) compares post-roll vs post-roll bars → ratio ≈ 1.0 → swallowed by the tolerance branch (`:425-432`). Seam permanently unrecorded. Next CL roll ~2026-07-20 (front month CLQ6).

### Amendment 1 (new latent defect) — `_apply_roll_to_cache` double-adjusts the overlap window
`data_manager.py:1052-1090`: cutoff recorded as `index.max()`, THEN the last ~3 days of cache bars are overwritten with new-basis IBKR bars; JIT replay multiplies every bar `< cutoff` — including those new-basis bars → `new_basis × ratio` (≈ old×r²), a spurious double step over a 3-day window. Never exercised (ledger always empty); corrupts the FIRST real roll the moment Component 2 is fixed.

### Amendment 2 — CL tolerance swallows real CL rolls
`src/core/instrument_master.py:81`: CL/MCL `roll_ratio_tolerance = 0.01`; real CL roll gaps run 0.2–2.8%, so sub-1% gaps would be silently tolerance-skipped even when correctly witnessed. All other symbols use 0.001.

### Amendment 3 — HourSet÷raw cannot derive post-HourSet seams
CL training data ends 2026-06-12; CL's ~Jun-18 seam is not recoverable from HourSet÷raw. Migration needs `DataBento/<SYM>/<SYM>_ratio.csv ÷ <SYM>_raw.csv` for ES/NG/GC/SI (downloads Jul 1–4 cover the June seams; must first gate ratio.csv ≡ HourSet basis on overlap) and a one-time IBKR expired-pair fetch (CLN6 vs CLQ6, includeExpired) for CL.

### Amendment 4 — restore-filter ownership makes naive backfill fragile
`data_manager.py:357-367` skips `roll_history` entries whose `"to"` doesn't `startswith(execution_symbol)` (current: CL/MES/MGC/NG/SIL). Backfilled entries must be prefix-correct today and would be SILENTLY DROPPED (basis reverts to raw, no error) if an execution symbol is ever swapped. Proposed: entries carry `"origin": "seed_backfill"` and the restore loop bypasses the ownership filter for them.

## Auditor's proposed fix — A + C combined, two independently-shippable stages (B rejected)

### Candidate verdicts
- **A (backfill migration): ACCEPT** — least invasive, restores training basis for embedded seams; insufficient alone (dies at next roll).
- **B (re-stage seeds on adjusted basis): REJECT** — breaks the "cache stays 100% RAW" split-brain invariant: execution/bracket-ATR reads the same cache, so SL/TP distances would scale by the cumulative factor (~10% error on NG); loses live-accumulated bars; still needs C anyway.
- **C (mid-run capture): ACCEPT with redesign** — anchor capture on the DATA basis switch (CONTFUT quotient scan), not the fleet's front-month poll; persist immediately; fail LOUD when unresolvable.

### Stage 1 — Migration (data only; no source edits)
New one-time `scripts/backfill_roll_history.py`:
1. Per symbol: quotient `q_t = HourSet_close / raw_close` on overlap, segmented; within-segment CV must be < 1e-6 or HARD FAIL.
2. Convert cumulative factors to per-roll ratios in the LIVE replay convention: `r_k = f_{k-1}/f_k`, cutoff = first bar of the new segment (`get_ratio_adjusted_df` masks `index < roll_ts`). Ratio DIRECTION is pinned by the replay-equality gate below, never by formula trust.
3. Post-HourSet seams per Amendment 3; every seam between seed start and NOW must be covered or the script HARD FAILS for that symbol.
4. Write entries with timestamped metadata backups first: `{"from", "to" (execution-symbol-prefixed), "ratio", "timestamp", "timestamp_cutoff", "origin": "seed_backfill_jit-roll-ratio-empty_07102026_1453"}`; recompute `cumulative_ratio` (informational only).
5. **Mandatory in-script validation gate:** offline `DataManager(data_client=None)` replay on scratch copies must show (a) `adjusted_close / HourSet_close` constant over every overlap timestamp (< 1e-6) ; (b) final cache bar byte-equal to raw; (c) feature-level spot check — `build_live_features()` on the adjusted frame reproduces HourSet stored features (at minimum `TS_VOL_YZ_ZSCORE_72v840` for NG) on overlap timestamps.
6. Activation = per-child restart; NG restarts first as live canary.

### Stage 2 — Live-code fix (requires canary per user rule; must land before ~2026-07-20)
Target files: `src/live_execution/data_manager.py`, `src/live_execution/live_trader.py`, `src/core/instrument_master.py`.
1. New `DataManager.resolve_roll_seam()`: CONTFUT fetch sized `detected_at − 2d → now`; per-bar quotient scan classifies old/new-basis bars; all-old → RETRY; all-new → escalate; old-run-then-new-run → ratio = median of old-basis run, cutoff = first new-basis bar; append via new shared `_append_roll_event()` helper and PERSIST immediately (persistence refactored out of `initialize()`).
2. `live_trader._check_contract_rollover`: persist a `pending_roll` record at detection; retry resolution on new 1h bars; loud (log.critical + Telegram) deadline escalation after ~3 days — never silent.
3. `initialize()`: unresolved `pending_roll` → resolve via widened fetch or HARD FAIL with operator remedy text (replacing the silent ratio≈1 swallow).
4. Amendment 1 fix: cutoff = first overwritten overlap timestamp (or drop the overwrite) + regression test asserting exactly one seam step.
5. Amendment 4 fix: restore loop unconditionally restores `origin == "seed_backfill"` entries.
6. Amendment 2 fix (separate reviewed line-item): CL/MCL tolerance 0.01 → 0.001.
7. Unit tests: seam-scan states (pre-flip retry, post-flip seam location, contaminated-tail median robustness), pending-roll restart resolution, hard-fail path, single-seam invariant.

### Rollout / rollback
- Stage 1 first; NG child restart as live canary (largest measured distortion; compare live shadow_log probs vs training-basis reconstruction); then fleet-wide coordinated restart. Stage 2 as a normal code change with canary before production; no working-tree edits while children run (restart window required).
- Contingency if Stage 2 slips past the CL roll: take the fleet down before IBKR's lead flip and restart after, letting the EXISTING startup path witness the roll.
- Rollback: Stage 1 = restore metadata backups + restart (today's behavior); Stage 2 = revert commit (schema back-compatible).

## Auditor's risk register
1. Intended behavior change: restored basis shifts live signals (the point) — NG shorts re-enable; watch sizing/threshold behavior during canary.
2. HourSet-choice mismatch: derive ratios from each child's configured dataset; CV gate protects; symbols sharing one metadata file but different training sets would need reconciliation (not the case today).
3. Ratio-direction inversion is the most dangerous migration bug — made unshippable by the replay-equality gate.
4. Databento ratio.csv basis must be proven ≡ HourSet basis before use; CL expired-contract fetch depends on IBKR serving CLN6 hourly history (verify).
5. Execution-symbol swaps orphan backfilled entries until Amendment 4 lands — document in migration output and preflight.
6. 5m managers restore the same entries (harmless for pre-cache cutoffs; correct inside range); no fleet child currently consumes 5m adjusted data for inference — verify before Stage 2 ships.
7. Fleet down beyond IBKR's fetchable window converts to a loud hard-fail — availability cost accepted deliberately over silent wrong-basis trading.
8. Out of scope, flagged: GC/ES always-above-threshold losses are a model-selection issue, not fixed here.
