# TDD Audit Log — cooldown-single-authority-wiring_07222026_1051

## Diagnosis chain (2026-07-22, from live SIL log)
1. SIL short opened 06:00 @ 59.41 (SL 60.23), SL_HIT 06:42, RE-SHORT 07:00
   @ 60.84 — config demanded max(sl_cooldown 7, short cooldown_bars 11) = 11
   hourly bars of lockout.
2. Virtual ledger ruled out (updated from the FRESH signal each bar,
   live_trader.py:5561-5574). Signal was genuinely Sell (0.5470 >= 0.54) —
   the failure was the un-armed gate, not a phantom signal.
3. Gate never armed because on_exit never fired: `hasattr(self, '_strategy')`
   always False (attr never assigned anywhere, ever — `git log -S
   "self._strategy = "` empty). Introduced already-broken in cafac9e
   (2026-06-18).
4. Bug was KNOWN: livetest_engine.py:726-730 "PARITY FIX" alias patched the
   HARNESS instead of production — parity runs validated a wiring that
   production did not have.
5. Cross-engine audit: BacktestEngine removed flavored tp/sl_cooldown_bars in
   3d95040 (2026-05-12; added 9096967 03-09) — three-way divergence
   (backtest: per-side only; livetest: flavored union; live: nothing).
   Flavored 7/0 values are hand-template hand-me-downs (dead
   execution_param_sweeper.py searched 0-6; 7 out of range); per-side values
   are Optuna-searched (strategy_optimizer.py:864, 1-13).
6. Test forensics: every wiring-adjacent stub hand-set `lt._strategy`
   (test_hourly_order_housekeeping.py:610 literally `lt._strategy =
   lt.strategy`); TestSeparateCooldowns vacuous (kwargs stripped by _bt +
   end-of-data open trades never recorded); test_time_barrier_no_cooldown
   truncated mid-body with zero assertions.

## Decisions
- Single authority = flavor-blind per-side cooldown_bars in BOTH engines
  (backtest already there; live gate rewritten to byte-mirror the
  TieredEnsemble resolution side_cfg -> top-level -> 0). Silent default
  sl_cooldown_bars=3 eliminated (no-silent-null-defaults).
- Wiring fix and gate consolidation land TOGETHER: wiring alone would have
  made live STRICTER than backtest wherever template 7 > per-side value
  (SI/GC/CL long side: 7 vs 1) — a brand-new parity break.
- Loud failure over guards: missing strategy/on_exit raises. The value of
  this choice was proven immediately — it surfaced two more prod-shaped stubs
  (test_settle_confirm_loop_deferral, test_live_trader_bugs) and one MORE
  silently-skipped production path (_recovery_stub: OOB recovery cooldown
  seeding had also been dead).
- Archived candidates/ configs keep their dead flavored keys (historical
  artifacts; unread after consolidation). Fleet configs stripped.
- models.*.threshold consolidation deferred to its own ticket (touches
  parity/sweep tooling readers with silent 0.5/0.55 defaults).

## Verification
- RED 6F/2P -> GREEN 8/8 (test_cooldown_wiring.py)
- Full suite 2546 passed / 1 skipped / 0 failed
- grep: zero `self._strategy` in src/; zero flavored-key reads in src/
  (comment mentions only); fleet configs carry per-side cooldown_bars only
