# Ticket Resolution Blueprint — cooldown-single-authority-wiring_07222026_1051
**Ticket Directory:** `.agents/collab/tickets/cooldown-single-authority-wiring_07222026_1051/`
**Authorization:** operator, 2026-07-22 ("I think we need to fix everything, so I agree with your recommendations and go ahead you can proceed") after the SIL incident diagnosis (stop-out 06:42, re-short 07:00 on the very next 1h bar despite a configured 11-bar short cooldown).

## Bug Summary
1. **Phantom-attribute wiring (SEVERE, live).** `LiveTrader.__init__` stores the
   strategy as `self.strategy` (live_trader.py:384) but both cooldown-arming
   sites read `self._strategy`, never assigned in production (introduced
   already-broken in cafac9e, 2026-06-18):
   - `_reset_position_state` (~:1337): `hasattr(self, '_strategy')` always
     False -> `strategy.on_exit()` silently skipped on EVERY TP_HIT/SL_HIT/
     TIME_BARRIER/OOB close -> no post-exit cooldown has EVER armed live.
   - `_seed_restart_cooldown` (~:2546): `getattr(self, "_strategy", None)`
     always None -> the entire restart-cooldown-recovery fix (ticket
     cooldown-not-restored-on-restart_07082026_0230) was inert in production.
   Masked three ways: unit stubs hand-set `lt._strategy`; the livetest parity
   harness aliased `trader._strategy = trader.strategy` ("PARITY FIX" comment,
   scripts/livetest_engine.py:726-730 — the bug was KNOWN and patched in the
   harness instead of production); silent hasattr/getattr guards never raised.
2. **Three-way cooldown semantics divergence.** BacktestEngine dropped the
   flavored `tp/sl_cooldown_bars` params in 3d95040 (2026-05-12; added
   9096967 2026-03-09) — since then the backtest enforces ONLY flavor-blind
   per-side `cooldown_bars` (TieredEnsembleStrategy re-gate reading engine
   `last_exit_bars_ago` counters, armed by `_close_trade` for EVERY exit
   reason). Live's ConfigurableStrategy gate still enforced
   `max(flavored sl/tp, per-side)` with a SILENT DEFAULT `sl_cooldown_bars=3`
   — stricter than the backtest wherever the hand-template 7 exceeded the
   Optuna-searched per-side value (e.g. SI long: 7 vs 1). Fixing (1) alone
   would have CREATED that live-vs-backtest divergence.
   Provenance: per-side `cooldown_bars` IS Optuna-searched
   (strategy_optimizer.py:864, range 1-13); `sl_cooldown_bars: 7 /
   tp_cooldown_bars: 0` are hand-written template hand-me-downs (the dead,
   never-imported agent/execution_param_sweeper.py searched only 0-6).
3. **Vacuous/broken tests.** tests/test_backtest_engine.py `_bt` silently
   STRIPPED all cooldown kwargs; `TestSeparateCooldowns` passed only because
   end-of-data open trades are never recorded (the "rejected" entry actually
   opened); `test_time_barrier_no_cooldown` was truncated mid-body with no
   assertions. Engine docstring still advertised the removed flavored params
   and a nonexistent COOLDOWN FSM state.

## Resolution (implemented under this ticket)
- **R1 wiring:** `_reset_position_state` calls
  `self.strategy.on_exit(side, reason, bars_held)` whenever
  `_position_side != 0`; `_seed_restart_cooldown` uses `self.strategy`.
  Silent guards REMOVED — a missing strategy/on_exit crashes loudly
  (no-silent-null-defaults). livetest_engine.py alias patch deleted.
- **R2 gate consolidation:** ConfigurableStrategy.evaluate's gate reads ONLY
  per-side `cooldown_bars` (resolution `side_cfg -> top-level -> 0`,
  mirroring TieredEnsembleStrategy exactly), flavor-blind — any exit reason
  arms it. `_last_exit_reason_long/short` fields removed (counter IS the
  armed state; the truthful reason still flows through on_exit to the
  execution strategy and the ledger). Counter semantics (reset -1, exit-bar
  reads 0, release exit+N+1), sentinel neutralization, and per-side advance
  rules UNCHANGED (B(b)+F convention preserved).
- **R3 configs:** `tp_cooldown_bars`/`sl_cooldown_bars` stripped from the 5
  fleet configs (HS14B CL, ES02B, NG01B, GC02B, SI01B). Optuna per-side
  values untouched (CL 1/1, ES 13/13, NG 3/5, GC 1/13, SI 1/11). Archived
  candidates/ configs left as historical artifacts (keys are dead vocabulary
  after R2). generate_batch_configs.py never wrote the flavored keys.
- **R4 tests:** new tests/test_cooldown_wiring.py (8 tests: wiring red->green,
  end-to-end SL-fill arming, source scans banning the phantom attribute and
  the harness alias). Re-adjudicated (marker comments in-file):
  test_parity_cooldown_single_authority.py, test_exit_bar_semantics.py,
  test_exit_reason_and_fill_routing.py, test_restart_cooldown_recovery.py
  (missing-strategy test now expects AttributeError), test_recovery_bars_held,
  test_oob_entry_state_recovery.py (A7 vocabulary class INVERTED: pins the
  flavored tuples' ABSENCE), test_oca_exit_ordering / test_oca_residual_
  detection / test_pending_roll_lifecycle / test_hourly_order_housekeeping
  (stub repairs: real `strategy` attribute only). test_backtest_engine.py:
  `_bt` strip-loop removed; TestSeparateCooldowns REPLACED by
  TestExecutionStrategyCooldown (4 tests pinning per-side cooldown_bars via
  the real TieredEnsembleStrategy path, every second trade explicitly closed
  so the end-of-data vacuity trap cannot fake a pass; flavor-blind TP and
  TIME_BARRIER arming pinned). Engine docstrings corrected.

## Live-behavior change (deploy note)
After deploy, fleet models will RESPECT per-side cooldown_bars they have been
silently ignoring since 2026-06-18: CL 1/1, ES 13/13, NG 3/5, GC 1/13,
SI 1/11 hourly bars locked out per side after ANY exit (SL, TP, time barrier,
OOB). This matches the backtest exactly (it always enforced these values), so
live joins the already-validated backtest behavior — but observed live trade
frequency will drop vs the broken pre-fix behavior. The former flavored union
(e.g. long side max(7, 1)=7) is GONE deliberately: it had no backtest
counterpart since 3d95040.
Deploy: rides the pending fleet restart (with 291a9fd + 394fa68). Canary:
livetest parity run on a fleet config (gate now byte-mirrors the backtest
re-gate resolution).

## Constraints honored
- No changes to execution_models.py, BacktestEngine behavior, engine counter
  semantics, or any Stage-2/Stage-4 OCA surface.
- ASCII-only operator strings; no try/except:pass added.
- Cloud pipeline untouched (no canary-triggering tree change:
  configurable_strategy.py is live/livetest-only; backtest_engine edits are
  docstring-only; test-only otherwise).
