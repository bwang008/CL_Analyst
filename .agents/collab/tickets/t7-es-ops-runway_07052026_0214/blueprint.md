# Ticket Resolution Blueprint — t7-es-ops-runway_07052026_0214
**Ticket Directory:** `.agents/collab/tickets/t7-es-ops-runway_07052026_0214/`

## Requirement Summary
T7 of the multi-symbol program: make `ES01B_Sharpe_E03_07042026.json` actually runnable
(`--dry-run` reaches the event loop with ES bars, zero CL requests). Combined scope
APPROVED by Impact-Reviewer with conditions C1-C7: (A) EQUITY session calendar (T5 C4
precondition), (B) hourly-only mode (USER RULING: no 5m data acquisition ever — the 5m
stream is vestigial for 1h configs; Databento is all-hourly), (C) ES 1h seed copy (ops),
(D) dry-run canary runbook (ops; USER GO-AHEAD required before execution).
Design docs: `audit.md` (calendar §1, runbook §3 — §2.2 5m provisioning SUPERSEDED),
`audit_hourly_only.md` (consumer map, mechanism, watchdog), `impact_review.md`
(verification + C1-C7). This document governs on conflict.

## Manager rulings (given)
- Mechanism: `live_config.enable_5m_stream`, optional, DEFAULT TRUE (CL byte-identical,
  zero config edits); loud startup log + Telegram stamp; `false` with `bar_size: 5m` →
  ValueError. Failure mode for unflagged seedless symbols stays the loud
  FileNotFoundError.
- Sequencing: equity calendar lands BEFORE the watchdog re-point (C3: pre-calendar,
  even 195-min thresholds false-positive at the Sunday reopen).
- The 3 T5 pin evolutions are SANCTIONED (C4 block in session_calendar.py:29-37
  pre-declared them): test_globex_family_dispatches_to_same_calendar drops ES/NQ;
  test_es_maintenance_break_modeled_closed re-pins to CLOSED-halt semantics;
  test_session_open_anchor_none_for_globex drops ES. C7: test_stale_threshold_pin_15
  is CLARIFIED, not flipped — 15 min stays pinned for 5m-enabled instances; a new
  135-min pin covers hourly-only instances.
- Trailing asymmetry (CL trails at 5m, ES at 1h) is the user-ruled intent — matches
  the 1h backtest exactly.
- ES CME entitlement: de-facto verified (real ES bars pulled 2026-06-27). GVZ: GC-only,
  out of scope.

## Target Files (code)
- `src/core/instrument_master.py` — `_EQUITY_SESSION = (("17:00","15:15"),
  ("15:30","16:00"))` on EXACTLY ES/MES/NQ/MNQ (micros move with parents — pinned).
- `src/live_execution/session_calendar.py` — third dispatch branch
  `_equity_market_status` (America/Chicago, grains pattern): open Sun 17:00 CT,
  halt 15:15-15:30 CT Mon-Fri, maintenance 16:00-17:00 CT (close Fri 16:00);
  equity `session_open_anchor` → most recent 15:30/17:00 CT open (Mon-Fri 15:30,
  Sun-Thu 17:00); C4 doc block updated (equity shape now modeled); GLOBEX/grains
  bodies untouched (T5 DST sweep + grains pins must stay green).
- `src/live_execution/live_trader.py` —
  (1) read `live_config.enable_5m_stream` (default True) at __init__; log loudly;
  ValueError when false + bar_size 5m;
  (2) when false: skip 5m DataManager construction/warm-start/cache-save/backfill and
  the brain 5m subscription (hands/front-month stream STAYS — order-pricing-critical:
  `_front_month_last_close` feeds current_price :3013 → marketable-limit entry :3425
  and time-barrier exits :1326-1330);
  (3) `_check_trailing_stop`: select the extremes frame ONCE at trigger time —
  `rolling_df_5m if it exists else rolling_df_1h` (C4: no flag read inside; the parity
  harness's populated 5m mirror stays byte-identical); monotonic max/min semantics
  unchanged;
  (4) None-guards at :726, :889 (C2: FUNCTIONAL — unguarded, the shared try swallows
  the AttributeError and SKIPS THE 1H CACHE SAVE), :2407;
  (5) watchdog: hourly-only instances anchor `_last_bar_time_1h` with threshold
  135 min (120 max normal oscillation + 15 margin); 5m-enabled instances keep the
  15-min/`_last_bar_time_5m` behavior byte-identical.
- `configs/strategies/ES01B_Sharpe_E03_07042026.json` — ONE field added:
  `live_config.enable_5m_stream: false` (verified tolerated by all T6 sentinels —
  only enumerated-field pins exist; T6 surgical-patch precedent).

## Ops steps (manager-run, NOT TDD)
- C5: copy `C:\CL_Analyst_Data\data\processed\ES_raw.parquet` → `ES_raw_1h.parquet`
  NEAR CANARY TIME (the now-anchored 4,638-bar window decays ~23 bars/trading day;
  file-max-anchored 4,696 ≥ 4,320 floor; no clobber — target verified absent).
- Canary (PARKED until explicit user go-ahead): `conda run -n trader python -m
  src.live_execution.cli --config configs/strategies/ES01B_Sharpe_E03_07042026.json
  --data-port 4002 --exec-port 4002 --dry-run` during ES hours (opens Sun 17:00 CT).
  cids 1010/1011 (no clash with HS14B's 1400/1401); paper account must be FLAT in ES;
  FRED_API_KEY set. Success: instrument-resolution log (ES/CME/tick 0.25), ES-named
  DATA PATHS, `Front-month contract: ES..`, 1h window ≥ 4,320 bars, subscriptions,
  heartbeat market=OPEN, NEW 1H BAR with plausible ES prices; C1 evidence via NEW 1H
  BAR + post-repoint heartbeat + telemetry `raw_front_month_bars` rows (front-month
  RAW BAR line is DEBUG — invisible in the INFO file log; do NOT bump it). Abort:
  any zero-CL grep hit (`symbol='CL'` | `\bCL[FGHJKMNQUVXZ][0-9]\b` |
  `Front-month contract: CL`), error 162/354, cid collision (326), real placeOrder,
  watchdog loop.

## Hard Constraints
1. CL byte-identical: no config edits; 5m path default-on; trailing at 5m; watchdog
   15-min; T5 GLOBEX DST sweep + grains pins + Q1 reopen pins all stay green.
2. No silent forks: mode explicit + logged; unknown session shape still raises.
3. C6 scope guards: do NOT fix the pre-existing :2527 vs :2253 "1h" inconsistency;
   NO generator emission of the new flag (T8); NO fleet_runner/backtest changes;
   deferred micro-tickets (CL 1h-stream watchdog, 1h telemetry rows) stay out.

## Test requirements (both audits' lists + C1-C7; highlights)
- Equity calendar frozen-clock (Jan+Jul CST/CDT): Tue 15:20 CLOSED-halt / 15:35 OPEN /
  16:30 CLOSED-maintenance / Sun 16:00 CLOSED / Sun 17:05 OPEN / Fri 15:35 OPEN /
  Fri 16:30 CLOSED-weekend / Sat CLOSED; anchor vectors; ES/MES/NQ/MNQ dispatch;
  the 3 sanctioned T5 pin evolutions + everything else in the T5 suite untouched.
- Hourly-only: ES-style config with enable_5m_stream false boots with ZERO 5m
  artifacts (no 5m DataManager, no 5m subscription, no 5m seed requirement); hands
  stream still subscribed; trailing reads 1h frame extremes (in-position 1h-bar
  scenario); the C2 cache-save guard (1h cache save happens despite absent 5m manager);
  watchdog: hourly-only 135-min anchor on _last_bar_time_1h (stale True at 140, False
  at 120), 5m-enabled 15-min pin intact; false + bar_size 5m raises; default-true CL
  config byte-identical construction (existing pins).
- ES01B: resolves AND carries enable_5m_stream false; T6 sentinel pins stay green.

## Verification
- Full fast suite green (baseline 1335 + new; exactly the 3+1 sanctioned T5 pin
  evolutions).
- BLOCKING: HS14B ledger parity gate (`setup --disable-trailing`, 2200/336) →
  PARITY: PASS before commit.
