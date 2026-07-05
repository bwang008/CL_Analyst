# Ticket Resolution Blueprint — t5-hours-watchdog-rollover_07042026_2305
**Ticket Directory:** `.agents/collab/tickets/t5-hours-watchdog-rollover_07042026_2305/`

## Requirement Summary
T5 of the multi-symbol live-gaps program: market-status, stale-bar watchdog, front-month
selection, roll-metadata, roll tolerance, and seed-lookback are CL-shaped. ZC launch
blockers: grain-halt reconnect storms (~6h/day), the guaranteed 4320-bar seed crash,
ratio-tolerance swallowing non-CL roll gaps, serial-month selection for metals.
HUMAN AUTHORIZED 2026-07-04 (as scoped) after Impact-Reviewer technical approval;
conditions C1–C5 binding. Full design: `audit.md` (§design, 27-item test list);
verification + conditions: `impact_review.md`. This document governs on conflict.

## Manager rulings (given)
- Q1: the pre-existing CL reopen/holiday watchdog false-positive stays PINNED AS-IS
  (follow-up ticket `cl-watchdog-reopen-grace_07052026_0001` minted; do NOT fix here).
- Q2: non-CL `roll_ratio_tolerance` = 0.001 ACKed (noise floor semantics verified:
  below tolerance = adjustment skipped, detection unaffected). CL/MCL = 0.01 (today's
  constant, pinned).
- Q4: ES/NQ have a REAL daily 15:15–15:30 CT halt not modeled by the GLOBEX calendar —
  C4: document in session_calendar.py + registry comment; equity session shape is a
  REQUIRED precondition for ES launch (T7 checklist), not built here (ZC is near-term).
- Q5: `month_str` stays `LTD[:6]` (telemetry comparability; consumers verified).

## Target Files
- NEW `src/live_execution/session_calendar.py` — GLOBEX calendar = current
  `_get_market_status` body moved VERBATIM (byte-identical status strings — sweep-pinned);
  GRAINS calendar (CT-based: overnight halt, 13:20 close, weekend); dispatch on registry
  `session_hours_ct` shape; unknown shape RAISES; `session_open_anchor` (GLOBEX → None →
  CL watchdog arithmetic bit-identical; grains → reopen anchor for watchdog grace).
  C4 documentation block for the ES/NQ 15:15–15:30 CT halt.
- `src/live_execution/live_trader.py` — `_get_market_status` delegates to the calendar
  (instance conversion preserves the `test_live_macro_refresh.py:80` mock seam);
  watchdog uses session-aware status + grains reopen grace (threshold stays 15 min);
  DataManager construction feeds registry `bars_per_day_5m/1h` (CL 288/24 pins);
  seed-lookback via formula `ceil(ceil(4320/bph_1h)*7/5)+28` (CL = 280 EXACT — pinned;
  ES 292; ZC 406); 4320-raise message cache-name-derived (CL text byte-identical).
- `src/live_execution/ibkr_client.py` — `_select_front_month` pure helper: active-month
  filter via `ContractDetails.contractMonth` (short-circuit when active_months covers
  all 12 — CL path never reads the field); LTD string-compare verbatim with registry
  `roll_buffer_days` (CL 6 identical; ES 8); FND-proxy (last weekday of prior month)
  for FND-referenced instruments; C3: GC/MGC holiday-guard — extend the proxy by
  skipping US-holiday Mondays OR bump GC/MGC roll_buffer_days so Memorial-Day-on-May-31
  years (next 2027) cannot leave the contract eligible past true FND (choose the
  simpler; document); loud raise when no active-month contract survives;
  `_EXPIRY_BUFFER_DAYS` deleted; remaining CL symbol defaults in front-month methods
  removed.
- `src/live_execution/data_manager.py` — `_ROLL_PRICE_TOLERANCE` constant → registry
  `roll_ratio_tolerance` (semantics documented: ratio-space noise floor); `bars_per_day`
  param becomes LIVE (consumed in seed math); roll-metadata C2 namespace fix:
  `last_front_month_by_symbol` + startswith ownership check, legacy key still written
  for CL (file semantics byte-identical); C1: `_save_roll_metadata`'s `old_fm` read uses
  the namespaced read order (no cross-symbol roll_history "from" contamination);
  C2(reviewer): Step-0 restore ownership-filtered (or concurrent parent+micro restore
  explicitly documented as unsupported — prefer the filter).
- `src/core/instrument_master.py` — new REQUIRED field `roll_ratio_tolerance` on all 15
  entries (CL/MCL 0.01; others 0.001); completeness test extends.
- Mechanical churn (census-verified): exactly 1 existing pin update
  (`tests/test_build_future_contract.py:390` — the self-documented T5 expiry-buffer pin).

## Hard Constraints
1. CL byte-identical everywhere: status strings (minute-by-minute DST-year sweep pin),
   watchdog decisions, front-month selection (buffer 6, no field reads), roll metadata
   file semantics, seed lookback 280, raise-message text. C5: these TDD pins are the
   ONLY regression fence (parity harness bypasses this layer) — they land WITH the code.
2. No silent defaults: unknown session shape raises; missing registry fields raise;
   no-active-month raises.
3. Scope guards: NO Q1 reopen-grace fix for CL (follow-up ticket), NO equity session
   shape build (T7 precondition, C4 doc only), NO generator (T6), NO fleet_runner,
   NO backtest engine.

## Test requirements (audit 27-item list + conditions; highlights)
- Frozen-clock: ZC Tue 15:00 CT → CLOSED-halt + watchdog False; ZC 08:00 CT reopen-grace;
  CL Sun 17:30 ET → weekend pre-open string byte-pin; CL minute-by-minute DST-year
  equivalence sweep vs the legacy body; ES maintenance-break modeled (16:00–17:00 CT
  CLOSED); C4 doc presence test optional.
- Front-month: GC serial-month filtering (mocked details list picks next active month);
  ES quarterly with buffer 8; CL selection byte-identical (buffer 6, active_months
  short-circuit); C3 2027 Memorial-Day case pinned; no-active-month raises.
- Rollover: ES ratio 1.004 IS applied (0.001 floor); CL 0.01 behavior unchanged;
  C1 roll_history "from" correctness under two-symbol writes; C2 Step-0 restore
  ownership filter.
- Seed math: CL 280 exact; ES 292; ZC 406 (no crash); registry bars_per_day consumed;
  4320-message CL byte-pin.
- Registry: roll_ratio_tolerance completeness + values.

## Verification
- Full fast suite green (baseline 1212 + new).
- BLOCKING: HS14B ledger parity gate (`setup --disable-trailing`, 2200/336) →
  PARITY: PASS before commit (LiveTrader/DataManager __init__ changed).
