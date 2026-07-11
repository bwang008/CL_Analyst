# Impact Review — jit-roll-ratio-empty_07102026_1453

Reviewer: Ticket-Impact-Reviewer | Date: 2026-07-10 | Decision: **REQUIRE HUMAN AUTHORIZATION** (Refactor Veto guardrail — mandatory escalation; NOT a technical rejection)

## 1. Independent verification of the Auditor's claims

Every code anchor in the review packet was independently read and confirmed accurate:

| Claim | Anchor | Verdict |
|---|---|---|
| JIT early-returns RAW copy when ledger empty | `src/live_execution/data_manager.py:1104-1106` | CONFIRMED |
| Ledger populated only from `roll_history` at `initialize()` | `data_manager.py:357-367` | CONFIRMED |
| `_detect_rollover()` returns False on first run | `data_manager.py:977-982` | CONFIRMED |
| Ratio capture only in `initialize()` Step 2; persistence only Step 5 | `data_manager.py:417-432`, `:449`; grep shows no other call sites of `_save_roll_metadata`/`_compute_roll_ratio`/`_apply_roll_to_cache` | CONFIRMED |
| Live rollover handler updates `front_month_id` in memory only | `live_trader.py:3679-3683` | CONFIRMED |
| 1h Brain stream is CONTFUT (`continuous=True`) | `live_trader.py:3488-3494` | CONFIRMED |
| Inference + warmup consume `get_ratio_adjusted_df()` | `live_trader.py:4325`, `:3273` — the ONLY two consumers in `src/` (grep-verified) | CONFIRMED |
| Amendment 1 double-adjust: cutoff = `index.max()` recorded BEFORE overlap bars are overwritten with new-basis IBKR data; JIT multiplies all bars `< cutoff` including the overwritten ones | `data_manager.py:1066-1088` vs `:1108-1112` | CONFIRMED — real latent defect, currently unreachable (ledger empty), armed the moment Stage 2 lands |
| Amendment 2: CL/MCL tolerance 0.01, all other symbols 0.001 | `instrument_master.py:81`, `:100` vs `:120-365` | CONFIRMED |
| Amendment 4: restore loop skips entries whose `"to"` doesn't startswith execution_symbol | `data_manager.py:358-363` | CONFIRMED (see §3.2 for a zero-code alternative) |
| All 5 metadata files `roll_history: []`, `cumulative_ratio: 1.0` | Sampled `C:\CL_Analyst_Data\data\processed\.roll_metadata.json` (CL, legacy name) and `.roll_metadata_NG.json` — both empty, `updated_at` 2026-07-10 00:52/00:54 (last fleet restart) | CONFIRMED |
| Split-brain invariant underpinning the Candidate B rejection: execution/bracket ATR reads RAW frames | `live_trader.py:1394-1396` (trailing logic reads `rolling_df_5m`/`rolling_df_1h` raw), `_on_new_bar` split-brain comment `:4320-4327` | CONFIRMED — B rejection is sound |

Root cause is real, the two-component decomposition is correct, and the four amendments are genuine (Amendment 1 in particular is a well-caught latent defect that would corrupt the first fixed roll).

## 2. Blast radius (independent map)

**Stage 1 (migration, data only):**
- Writes: 5 metadata JSONs under `C:\CL_Analyst_Data\data\processed\` (with timestamped backups). New script `scripts/backfill_roll_history.py`. Zero source edits.
- Activation surface: at each child restart, the adjusted feature frame for ALL FIVE 1h fleet children changes basis — i.e., live signal behavior changes fleet-wide (this is the intended fix, but it IS a production trading behavior change effected through data, not code).
- Execution path untouched: bracket/trailing ATR and order placement read raw frames (`live_trader.py:1394-1396`); SL/TP distances do not move.
- Reversible: restore backups + restart.
- 5m managers share the metadata file: pre-cache cutoffs are no-ops; no current fleet child consumes 5m adjusted data for inference (proposal correctly requires re-verification before Stage 2).

**Stage 2 (live code):**
- `src/live_execution/data_manager.py` — new `resolve_roll_seam()` + `_append_roll_event()`, persistence refactored out of `initialize()`, hard-fail path replacing the tolerance swallow, `_apply_roll_to_cache` cutoff semantics change. This class is instantiated by every fleet child (5m and 1h managers).
- `src/live_execution/live_trader.py` — `_check_contract_rollover` gains a persisted `pending_roll` lifecycle with retry + loud deadline escalation.
- `src/core/instrument_master.py` — CL/MCL tolerance constant. Grep-bounded: `roll_ratio_tolerance` is consumed ONLY by live `data_manager.py` (`:321`, `:425`, `:876`, `:1209`) — no training/backtest consumer, so the constant change is live-scoped.
- Metadata schema gains `origin` and `pending_roll` keys — verified back-compatible both directions (old reader ignores unknown keys; new entries carry prefix-correct `"to"` so they restore even without the Amendment-4 code).
- **Test blast radius the proposal does NOT inventory:** `tests/test_session_watchdog_rollover.py:834-860` pins tolerance values including the explicit `assert _dm("CL").roll_ratio_tolerance == 0.01` (`:859`) and an expected-value map (`:844`) — Amendment 2 breaks these deliberate T5 "zero-change pin" assertions and reverses a previously reviewed decision. Also in scope of behavioral assertions: `tests/test_data_manager_ratio.py`, `tests/test_rollover.py`, `tests/test_data_manager.py`, `tests/test_feature_parity.py`.

## 3. Constraint evaluation (workflow rules)

1. **Interface Rule** — NOT triggered in the strict sense: no existing public signature changes; new methods are additive; persisted schema change is back-compatible. The restore-loop semantics change (Amendment 4) is a data-contract change but compatible both directions.
2. **Base Class Rule** — TRIGGERED (softly): `instrument_master.py` is the core registry and `data_manager.py` is a core live utility shared by every fleet child. Business justification is strong and specific: the Auditor enumerated localized alternatives (A/B/C), rejected B for reasons I independently verified against the split-brain code, and the CL tolerance at 0.01 demonstrably defeats the entire roll-capture mechanism for CL (real gaps 0.2–2.8%). Approvable under the exception — if rule 3 did not apply.
3. **Refactor Veto — TRIGGERED.** Stage 2 is a coordinated behavioral rewrite across TWO live-critical components (DataManager: seam-resolution machinery, persistence refactor, initialize hard-fail; LiveTrader: pending_roll persistence/retry/escalation lifecycle) plus a core-registry change. Under the Mandatory Human Authorization Guardrail I am strictly forbidden from autonomously approving a multi-component refactor regardless of justification quality. **Escalating to the human user via the Ticket-Manager.**

Independent operational reinforcers of escalation (beyond the mechanical rule): the fleet is RUNNING from this checkout, so both stages require human-operated restart windows; the user's canary rule applies to Stage 2; Stage 1 intentionally changes live signals (NG shorts re-enable while positions may be open); and the ~2026-07-20 CL roll deadline forces a human scheduling decision between "land Stage 2 with canary in ~10 days" and the proposal's own contingency (fleet down across IBKR's lead flip, letting the existing startup path witness the roll).

### 3.1 Riskiest element (my independent assessment)
**Stage 2's persist-immediately mid-run seam capture.** A mis-located old/new-basis boundary writes a durable wrong ratio+cutoff into metadata that every subsequent restart replays — the failure mode is permanent and self-propagating, unlike today's (also bad, but known) raw-basis state. Mitigations are credible (same-timestamp quotients cancel market moves; median over the old-basis run; contaminated-tail tests), but note: a genuine roll gap smaller than the tolerance classifies every bar "new-basis" → the all-new escalation path fires — that path is therefore load-bearing and must be loud AND actionable, not just a log line. Stage 1's nominally scariest bug (ratio-direction inversion) is effectively fenced by the mandatory replay-equality + feature-level gate (5a–c) and I consider it well-mitigated AS LONG AS the gate stays HARD FAIL.

### 3.2 Findings for the Auditor / human (attach to authorization decision)
1. **Stage 1 can be decoupled from Stage 2's Amendment-4 code change with zero code:** the restore loop's legacy branch (`data_manager.py:360` — `isinstance(to_fm, str)` guard) already unconditionally restores entries that LACK a `"to"` string. Writing backfill entries without a `"to"` key (contract label under a different key, e.g. `"to_contract"`) makes them execution-symbol-swap-proof TODAY. The Auditor's `origin`-marker design is more self-documenting; either is acceptable, but the proposal should state which and why, since Stage 1 as drafted carries a known-fragile window until Stage 2 lands (risk #5).
2. **Amendment 2 must include updating the pinned tests** at `tests/test_session_watchdog_rollover.py:844/:859` and should explicitly acknowledge it reverses the T5 "zero-change pin" decision (that pin was reviewed; un-pinning deserves the same visibility).
3. Stage 1's validation gate (replay-equality + feature-level reproduction) is the load-bearing safety mechanism and must remain HARD FAIL per the no-silent-null-defaults rule — no tolerance widening to "make migration pass."
4. NG-first canary ordering is correct (largest measured distortion, 10/48 signal flips); require the shadow_log-vs-training-basis comparison to be recorded in the ticket before fleet-wide restart.
5. Deadline pressure (~2026-07-20) must not be used to skip the Stage 2 canary; the proposal's own contingency (scheduled fleet downtime across the lead flip) is the correct pressure-relief valve.

## 4. Decision

**REQUIRE HUMAN AUTHORIZATION.** The proposal is technically sound — every claim verified, candidate analysis correct, rollback paths real, validation gates well-designed — but Stage 2 is a multi-component refactor of two live-critical components plus a core registry, which mandates human sign-off under the guardrail; the live-fleet restart scheduling, signal-behavior change, and CL-roll deadline tradeoff are human decisions in any case. Recommended framing for the human: authorize Stage 1 (data-only, gated, reversible, NG canary first) and separately authorize Stage 2 scope + its canary/restart window before ~2026-07-20, or elect the fleet-downtime contingency for the CL roll.
