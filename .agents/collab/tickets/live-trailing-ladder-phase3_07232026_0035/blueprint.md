# live-trailing-ladder-phase3_07232026_0035 — live multi-rung trailing ladder

**Operator authorization 2026-07-23 ~00:35 PT:** implement Phase 3 of the
trailing-ladder ticket (trailing-stop-ladder_07132026_1745): the LIVE trader
executes multi-rung `trailing_ladder` configs, removing the startup guard
that blocked NQ E04 tonight (RuntimeError at live_trader.py ~408-420).
Operator confirmed the rung-derivation semantics are the intended ones
("midpoint between last trigger and TP, ratchet SL to the previous
trigger" = the optimizer's `trigger2 = a1 + 0.5*(TP - a1), lock2 = a1`;
verified against the NQ config: long 6.525/5.8, short 4.35/1.45).
NQ stays parked (`enabled: false`, operator-managed) until this ships.

## Authority: the backtest engine's semantics (MUST match exactly)

`agent/backtest_engine.py:1047-1065` (and its twin at 1136+): per bar,
ADVANCE through the rung list while the favorable extreme has crossed the
rung's activation:

    while rung < len(ladder):
        act, lock = ladder[rung]
        long:  if highest_high < entry + act*ATR_at_entry: break
               sl = entry + lock*ATR_at_entry
        short: if lowest_low  > entry - act*ATR_at_entry: break
               sl = entry - lock*ATR_at_entry
        rung += 1

- Rungs are validated monotonic (activation strictly increasing) by
  `_normalize_trailing_ladder` (execution_models.py:122) /
  `strategy_config.parse_trailing_ladder`; multiple rungs can be consumed
  in ONE bar (a gap through 2 activations locks the highest).
- Exit reason: a stop fill with rung>0 books TRAILING_BE (engine 984/992/
  1070/1357/1365). Live equivalent: `_trailing_activated` True — feeds the
  TRAILING_BE remap in `_reset_position_state` and ledger
  `trailing_activated`.
- The legacy single-rung path (`trailing_atr_mult` + offset) must stay
  BYTE-IDENTICAL for the 5 running fleet configs (no ladder key).

## Implementation sites (live)

1. `LiveTrader._check_trailing_stop` (live_trader.py ~1569-1750):
   - Resolve the side's ladder from StrategyConfig (`_sc.long/short.
     trailing_ladder`) at __init__ (store per side, like the offsets).
     No ladder -> EXACTLY today's single-rung behavior (identity fence).
   - Replace the single-shot `if self._trailing_activated: return` latch
     with rung-aware logic. RECOMMENDED STATELESS DESIGN: each bar compute
     the HIGHEST activated rung from the extremes (same while-loop),
     target_sl = entry +/- lock*ATR, `round_to_tick`; transmit a modify
     ONLY when target is STRICTLY TIGHTER than `_tracked_sl_price`
     (long: >, short: <) — the tracked price (ledger-restored) makes this
     restart-safe and never-loosening without a new persisted rung
     counter. Keep the existing no-op skip guard (half-tick tolerance +
     `_trailing_activated` re-latch) — it becomes the "current rung
     already resting" case naturally.
   - `_trailing_activated = True` whenever any rung's lock is resting
     (i.e., after any successful ladder modify, and via the skip-guard
     re-latch).
   - Known inherited limitation (document, do NOT fix here): extremes
     (`_highest_high/_lowest_low`) reset on restart, so a rung whose
     activation was touched ONLY before a restart re-arms when price
     re-crosses. Same behavior as today's single rung. The tracked-SL
     comparison guarantees already-locked rungs never loosen.
2. Remove the BACKTEST-ONLY GUARD (live_trader.py ~408-420) — LAST commit
   step, only after tests + parity pass.
3. `_seed_restart_cooldown` / recovery: no change needed (reason mapping
   is downstream of `_trailing_activated`/ledger column, both already
   maintained).

## Tests (TDD — write first)

New tests/test_live_trailing_ladder.py (mirror the object.__new__ stub
pattern of tests/test_log_cosmetics.py::TestNoOpTrailingSkip, incl.
`_instrument_context` tick seam and `_tracked_sl_price`):
- No-ladder config: behavior byte-identical to today (single rung fires
  once, latch honored) — identity fence.
- 2-rung long: rung1 activation crossed -> modify to entry+lock1*ATR;
  later rung2 crossed -> second modify to entry+lock2*ATR; tracked price
  updated each time; `_trailing_activated` True from the first.
- Gap through both activations in one bar -> ONE modify straight to
  rung2's lock.
- Never-loosen: tracked SL already tighter than the computed target ->
  no transmit (skip guard path).
- Short-side mirror of the rung1->rung2 case.
- Restart mid-trade: fresh stub with tracked SL = rung1 lock (ledger
  restore), extremes reset; price re-crosses rung2 -> modifies to rung2;
  price only re-crosses rung1 -> NO modify (never loosen).
- Guard removal: a 2-rung config constructs a LiveTrader without raising
  (the old guard test — find and re-adjudicate it: grep
  "backtest-only\|multi-rung" in tests/, likely pinned by the
  trailing-stop-ladder ticket's suite).

Suite: `conda run -n trader python -m pytest tests/ -q -m "not slow"`
must be delta-clean vs the pre-change baseline (run baseline FIRST;
2654 passed / 1 skipped as of 586bc33).

## Constraints (hard)

- Do NOT touch configs/, the fleet manifest, or anything under
  .agents/collab/error_queue/. Do NOT restart or signal the fleet.
- Trader env for ALL pytest/python (`conda run -n trader ...`).
- No cheap fixes (no try/except-pass, no silent defaults, no loosened
  tests); mechanical stub repairs must cite this ticket id in a comment.
- Commit on `development`, message
  `feat(live-trailing-ladder-phase3_07232026_0035): <summary>` with
  "deploy pending operator fleet restart" in the body; stage file-by-file
  (operator WIP may be present in configs/ — NEVER `git add -A`).
- Multi-line commit message via no-BOM file + `git commit -F`.

## Definition of done

Tests green (new + full fast suite delta-clean), guard removed, committed.
Livetest/parity canary vs the engine is the operator's post-restart step
(/validate-parity) — note it in the completion report, do not run the
fleet. Report: files changed, test counts, commit sha, and any deviation
from this blueprint with reasoning.
