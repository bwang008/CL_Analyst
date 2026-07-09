# Impact Review — recovery-barsheld-wallclock_07092026_1239

**Reviewer:** Ticket-Impact-Reviewer
**Date:** 2026-07-09 12:46
**Verdict:** APPROVE (with binding implementation conditions C1-C5 below)

---

## 1. Proposed fix under review

One new private helper `LiveTrader._bars_since(ts) -> Optional[int]` counting BRAIN-stream
bars strictly after `ts` from the rolling df matching `self._bar_size` (`rolling_df_1h` for
"1h", `rolling_df_5m` for "5m"; None/empty df → None). Replace the two wall-clock
`int(delta_minutes / bar_dur)` estimates:

- **Site 1** — position recovery, `src/live_execution/live_trader.py:1960-2001`
  (estimate at :1965-1977): `_position_bars_held = _bars_since(entry_bar_time)`;
  on None keep the existing conservative default (0).
- **Site 2** — `_seed_restart_cooldown`, `src/live_execution/live_trader.py:1735-1753`:
  `bars_elapsed = _bars_since(close_time)`; on None stay inert (existing behavior).

## 2. Code verification (all claims checked against source)

- **Both sites confirmed** at the stated shapes. Site 1: :1964-1977 computes
  `delta_minutes` from `rolling_df_5m.index[-1]` minus the ledger `entry_bar_time`,
  divides by `_bar_minutes[bar_size]`. Site 2: :1745-1753 same division against
  `pd.Timestamp(close_time)`. Both count phantom bars across the ~49h weekend gap and
  the daily halt — the bug is real.
- **Steady-state semantics match**: :1639-1643 sets `_position_bars_held = 0` at the bar
  where the position is first tracked; :1645 does `+= 1` per received brain bar. So the
  true counter equals "number of brain bars strictly after `entry_bar_time`" — exactly
  `(df.index > ts).sum()`. **Off-by-none**: the strict `>` excludes the entry bar, which
  the steady-state counter also never counts (it sets 0 there). The first live bar after
  restart increments the recovered count exactly as a continuously-running bot would.
- **Site 2 seeding convention preserved**: docstring :1722-1727 — seed
  `_last_exit_bars_ago = bars_elapsed - 1`, the gate's pre-increment
  (configurable_strategy.py:422-427) then reads the honest count. The fix only changes
  HOW `bars_elapsed` is obtained; the `-1` convention and `bars_elapsed > 0` guard
  (:1758-1762) are untouched.
- **Startup ordering safe**: `start()` runs `_warm_start()` at :937 (seeds the rolling
  dfs) BEFORE `_recover_inherited_position()` at :945 (which calls
  `_reconstruct_cooldown_from_ledger` at :1858 and `_seed_restart_cooldown` at :1914).
  For 1h/2h/4h configs, warm start hard-fails unless `rolling_df_1h` has
  ≥ REQUIRED_1H_BARS = 4320 bars (:3218-3235; data_manager.py:114). For 5m configs,
  `rolling_df_5m` must be non-empty or startup raises (:3135-3138). So whenever the
  recovery paths run in production, the brain-matched df is present and ~6 months deep —
  vastly deeper than fleet max_hold (per-side 6-36) or any cooldown window.
- **"ts predates seeded window" edge**: count saturates at window length → a LOWER bound
  → under-counts bars held / under-ages the cooldown → errs toward HOLDING the position
  and toward BLOCKING re-entry. Both are the conservative direction. With a 4320-bar
  window this edge is practically unreachable.
- **Timestamp domains consistent**: live bar index is tz-naive UTC (:4188-4190
  `tz_convert("UTC").tz_localize(None)`); `_utc_iso_now()` is tz-naive UTC (:1157-1158);
  `entry_bar_time` is stored as `isoformat()` of a bar-index value (:5886). Comparisons
  are same-domain by construction; no new skew vs the current subtraction.
- **Hourly-only instances do not regress — they improve**: site 1 is currently gated on
  `rolling_df_5m is not None` (:1965), so hourly-only instances (enable_5m_stream=false,
  rolling_df_5m stays None per :3131/:508) NEVER estimate and recover with
  bars_held = 0. The helper routes "1h" → `rolling_df_1h`, giving them an honest count
  for the first time. Site 2 already falls back to `rolling_df_1h` (:1741-1742).
  The presence-based dispatch pinned by tests/test_hourly_only_equity_session.py
  (:1389-1396 trailing df selection) is untouched.
- **Site 2 subtle improvement**: current code takes "now" from the 5m df even on 1h
  configs (:1739-1740), which can be up to 55 min fresher than the last 1h bar; the
  helper counts actual 1h brain bars — the parity-correct unit.
- **Fleet exposure validated**: all 5 manifest instances (HS14B/ES01B/NG01B/GC01B/SI01B)
  are `bar_size: "1h"` with per-side max_hold 6-36. A weekend adds ~49 phantom "bars"
  under the current estimator — exceeds every per-side max_hold in the fleet →
  the spurious Sunday-open TIME_BARRIER close is real for every symbol. Severity HIGH
  confirmed.

## 3. Blast radius

**Files modified (production):** `src/live_execution/live_trader.py` only — one new
private helper + two call-site edits.

**Value consumers of `_position_bars_held`** (int in, int out — only accuracy changes):
time-barrier compare :1652; `on_exit` third arg :1180/:1755; telemetry `bars_held`
columns :1353/:1539/:1592/:1688/:3015/:5641/:5770; Telegram :5653; trailing paths
:4535/:4544. No type/shape/interface change.

**Cooldown fields `_last_exit_bars_ago_long/short`:** internal to ConfigurableStrategy
(configurable_strategy.py:92-93, 422-427, 448-456, 597-600) plus the two seeding writes
in live_trader (:1759-1762). No external module reads them.

**Inheritance/interfaces:** no subclasses of LiveTrader exist (only `LiveTrader.__new__`
test stubs); neither modified method is called from outside live_trader.py + tests;
`_bars_since` is new and private.

**Tests that pin the OLD wall-clock math (must be updated in this ticket):**
- `tests/test_restart_cooldown_recovery.py` — fixtures seed a single-bar
  `rolling_df_5m` with `bar_size="1h"` and assert wall-clock-division values
  (`test_historical_exit_seeds_honest_bars_ago`, `test_aged_out_row_is_inert`,
  Part 2 reconstruction tests, `test_unknown_bar_size_does_not_raise`). Under the fix,
  "1h" reads `rolling_df_1h`, so fixtures must seed real hourly bars and expectations
  become bar counts; the unknown-bar-size test becomes an inert-on-unknown pin.
  The inert guards (`test_no_bar_time_stays_inert`, `test_missing_strategy_is_safe`,
  `test_none_reason_not_armed`, ledger-failure tests) remain valid as-is.
- `tests/test_execution_parity.py::TestRecoveryBarsHeld` — self-replicates the old
  estimator and its comment claims it "must match live_trader.py recovery code". It
  would still pass (self-contained) but would then assert a contract that no longer
  exists. Retire or rewrite it against the new bar-counting contract.
- `tests/test_oob_entry_state_recovery.py` exercises the OOB branch which returns at
  :1917 BEFORE the site-1 estimator — unaffected.

## 4. Constraint rules

1. **Interface Rule — NOT TRIGGERED.** No existing function signature changes.
   `_bars_since` is a new private helper consumed only inside LiveTrader.
2. **Base Class Rule — NOT TRIGGERED.** LiveTrader is a leaf application class with no
   subclasses; live_trader.py is not a widely-inherited base/core utility.
3. **Refactor Veto — NOT TRIGGERED.** One production file, one helper, two localized
   call-site edits, plus updates to the tests that directly pin those call sites.
   Single component.

**Human-authorization guardrail:** the fix touches live execution. The human user has
explicitly requested this ticket be opened AND resolved — authorization is on record;
no halt required.

## 5. Binding implementation conditions

- **C1 — Unsupported bar sizes return None, loudly.** For any `_bar_size` not in
  {"5m", "1h"} (notably "2h"/"4h", whose brain bars are RESAMPLED from `rolling_df_1h`
  at :4234-4251 — counting raw 1h rows would over-count 2-4x and recreate the exact bug),
  `_bars_since` must return None and log a warning (no-silent-fork house rule). Zero
  live impact today: no deployed 2h/4h configs (fleet manifest is all 1h).
- **C2 — Exception safety.** The helper must not raise on malformed `ts`
  (`pd.Timestamp` parse failure, tz-aware/naive comparison mismatch) — catch → None; or
  the call sites must retain their existing try/except (site 1's estimate is wrapped
  :1962-1987; site 2 try/excepts the parse :1747-1752). A restart-recovery path must
  never crash startup.
- **C3 — Exact counting semantics.** Strictly-greater `(df.index > ts).sum()` (verified
  off-by-none vs the :1639-1645 steady-state counter). Site 2 keeps the
  `bars_elapsed - 1` seeding and `bars_elapsed > 0` guard byte-identical.
- **C4 — Re-pin the tests.** Update `tests/test_restart_cooldown_recovery.py` fixtures
  to seed `rolling_df_1h` for 1h cases; retire/rewrite
  `tests/test_execution_parity.py::TestRecoveryBarsHeld`; add gap-scenario pins
  (Friday entry + weekend gap → recovered count stays ≤ true bar count, no spurious
  TIME_BARRIER; cooldown not over-aged across the gap).
- **C5 — Minimal diff.** Do not touch the neighboring `_position_entry_bar_time`
  fallback assignments (:1988-2001), the trailing-extremes init (:2004-2006), or the
  5m-preference in site 2's current-time probe beyond what the helper replaces.

## 6. Decision

**APPROVE.** The fix is gap-immune by construction, matches steady-state bar-counting
semantics exactly, fails toward the conservative side on every unmeasurable edge
(hold longer / block re-entry longer), improves hourly-only instances that currently
never estimate at all, and is confined to one file with no interface, base-class, or
multi-component exposure. Human authorization for touching live execution is on record
from the user. Conditions C1-C5 are binding on the Auditor's implementation.
