# Ticket Resolution Blueprint — recovery-barsheld-wallclock_07092026_1239
**Ticket Directory:** `.agents/collab/tickets/recovery-barsheld-wallclock_07092026_1239/`

## Bug Summary
Two restart-recovery sites in `src/live_execution/live_trader.py` convert wall-clock elapsed
time into bar counts (`int(delta_minutes / bar_dur)`). Market gaps (weekend ~49h, daily 1h
halt, holidays) contain zero bars, so both sites over-count across gaps:

1. **Position recovery (`:1964-1977`)** — over-estimates `_position_bars_held`. A Friday
   position recovered after a weekend restart computes ~52 phantom "bars" vs a true ~3; every
   fleet config's per-side `max_hold_bars` (6–36) is exceeded → **spurious TIME_BARRIER close
   at Sunday open**. Daily halt adds +1 phantom bar/day.
2. **`_seed_restart_cooldown` (`:1745-1753`)** — over-estimates `bars_elapsed` → restored
   re-entry cooldowns age out too fast across gaps → re-entry earlier than backtest parity.

Steady-state counting (`:1639-1645`, `+= 1` per received brain bar) is correct; only the
restart estimators are gap-blind. Severity HIGH; not a recent regression; human authorization
to modify live execution is on record (user requested open + resolve).

## Target Files
- `src/live_execution/live_trader.py`
- `tests/test_recovery_bars_held.py` (new)
- `tests/test_restart_cooldown_recovery.py` (re-pin fixtures per C4)
- `tests/test_execution_parity.py` (`TestRecoveryBarsHeld` retires/rewrites per C4)

## Required Changes (per approved proposal + reviewer conditions C1–C5)
1. Add private helper `LiveTrader._bars_since(ts) -> Optional[int]`:
   - Selects the rolling df matching `self._bar_size`: `"1h"` → `rolling_df_1h`,
     `"5m"` → `rolling_df_5m`.
   - **C1**: any other bar_size (2h/4h are RESAMPLED from 1h — raw row counting would
     over-count 2–4×) → log a warning and return None.
   - Returns `int((df.index > pd.Timestamp(ts)).sum())` — **C3** strictly-greater, matching
     the steady-state counter's off-by-none semantics (entry/close bar excluded).
   - **C2**: full no-raise safety — malformed/incomparable `ts`, None/empty df → None (never
     crash startup recovery).
2. Site 1 (position recovery): replace the delta-minutes estimate with
   `_bars_since(entry_bar_time)`; on None keep the existing conservative default (0).
   **C5**: do NOT touch the `_position_entry_bar_time` fallbacks (`:1988-2001`) or
   trailing-extremes init (`:2004-2006`).
3. Site 2 (`_seed_restart_cooldown`): replace `delta_min / bar_dur` with
   `_bars_since(close_time)`; on None stay inert (return). **C3**: the `bars_elapsed - 1`
   seeding and `bars_elapsed > 0` guard stay byte-identical.
4. Tests (TDD):
   - New weekend-gap pins: Friday entry + post-weekend recovery yields the true small bar
     count (not ~52) → no spurious TIME_BARRIER; same for cooldown seeding across a weekend.
   - Off-by-none pin vs steady-state counter; ts-predates-window lower-bound behavior;
     non-{5m,1h} bar_size → None + warning; malformed ts → None.
   - **C4**: re-pin `tests/test_restart_cooldown_recovery.py` fixtures (seed `rolling_df_1h`
     for `bar_size="1h"`, not a single-bar `rolling_df_5m`); retire/rewrite
     `tests/test_execution_parity.py::TestRecoveryBarsHeld` (it self-replicates the old
     wall-clock math).
5. Full fast suite green (10 pre-existing ES01B sentinel failures expected/unrelated).
