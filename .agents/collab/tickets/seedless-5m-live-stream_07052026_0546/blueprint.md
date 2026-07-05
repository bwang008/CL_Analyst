# Ticket Resolution Blueprint — seedless-5m-live-stream_07052026_0546
**Ticket Directory:** `.agents/collab/tickets/seedless-5m-live-stream_07052026_0546/`

## Requirement Summary
USER-DECIDED design (clarified ruling: only HISTORICAL 5m purchases are banned; every
live model should subscribe to live 5m bars for trailing granularity): the 5m
warm-start seed requirement now follows what the model consumes. Seed/cache exists →
byte-identical (CL untouched). Neither exists + hourly model → shallow IBKR bootstrap
(single "5 D" fetch), loud 3-surface disclosure, self-healing via first-run cache
save. Neither exists + 5m model → the exact FileNotFoundError survives (dormant
NaN-feature guard). ES01B's `enable_5m_stream: false` key is REMOVED (flag stays as
explicit opt-out). Reviewer verdict: APPROVE, conditions C1-C6. Design: `audit.md`
§5; verification: `impact_review.md`. This document governs on conflict.
**Cite HEAD `f165b9d`** (336d29f + other sessions' doc/data commits; zero source drift).

## Also in scope (manager-sanctioned repair)
Commit 336d29f flipped ES01B to MES/cid-2000 without evolving its sentinel pins —
**6 tests fail at HEAD** (5× 'MES'=='ES', 1× 2000==1010, reviewer-confirmed by run).
Repair them here, evolved to MES/ES/CME/2000, each citing this ticket ID.

## Target Files
- `src/live_execution/data_manager.py` —
  `__init__(..., allow_shallow_bootstrap: bool = False)` (keyword-only) +
  `self.shallow_bootstrapped` attr; `initialize()` Step-1 else-branch: if allowed AND
  `self.data_client is not None` → `_shallow_bootstrap_from_ibkr()`: ONE non-chunked
  `fetch_historical_bars_by_duration(duration_str="5 D", continuous=True, ...)` via
  the (symbol-bound) adapter, `_drop_incomplete_bar`, **RuntimeError on empty BEFORE
  any save_cache()** (C3), then `save_cache()` (run 2+ = normal cache warm-start);
  otherwise the **byte-identical** FileNotFoundError (C2 — also: allow=True with
  data_client None still raises it); Step-4 training-ledger accrual GATED while
  seedless (`shallow_bootstrapped` or ledger-absent-and-seed-absent per audit §5) with
  a loud skip log — run 2+ (cache present, seed absent, ledger absent) must boot
  cleanly (C5: explicitly pinned).
- `src/live_execution/live_trader.py` — pass
  `allow_shallow_bootstrap=(self._bar_size in ("1h","2h","4h"))` to the 5m DataManager
  construction; loud `SHALLOW 5M BOOTSTRAP` warning + `_warm_start` banner + Telegram
  startup `Mode:` stamp when it fires (3-surface discipline, mirrors HOURLY-ONLY).
- `configs/strategies/ES01B_Sharpe_E03_07042026.json` — REMOVE the
  `"enable_5m_stream": false` line (SAME COMMIT as the code — C1: the fleet manifest
  has ES01B enabled; a key-only removal crash-loops on restart).
- Test evolutions (7, all sanctioned, cite this ticket):
  `tests/test_hourly_only_equity_session.py::TestES01BFlagPatch::
  test_es01b_carries_enable_5m_stream_false` → RENAMED (C4, e.g.
  `test_es01b_no_longer_carries_enable_5m_stream_key`) asserting the key is ABSENT;
  the 6 broken MES pins across `test_hourly_only_equity_session.py` /
  `test_instrument_context.py` / `test_config_generator_symbols.py` → MES/ES/CME/2000.
- Docs guidance amendment (audit §5 enumerated lines): build-symbol-pipeline.md
  Phase 1, run-live.md preflight, add-remove-fleet-model.md, headless-deployment.md /
  systemd README — "new symbols need no 5m seed; the 5m stream bootstraps shallow
  automatically; enable_5m_stream:false remains an explicit opt-out only".

## Hard Constraints
1. CL byte-identity: seed+cache path untouched; the new param default-False is
   byte-neutral at all construction sites (reviewer census: 2 src + ~30 tests).
2. No silent forks: shallow mode loudly disclosed on 3 surfaces; empty fetch =
   RuntimeError (never an empty window); 5m-model raise message byte-identical.
3. T7 synthetic flag-false fixtures stay green (the flag remains a valid opt-out).
4. Scope guards: NO fleet_runner/generator/backtest/ibkr_client changes.

## Test requirements (audit §5 list + conditions; highlights)
- CL-with-seed pins (byte-identical construction + no bootstrap call).
- Shallow bootstrap happy path (mocked adapter returns 5 D of bars → window built,
  cache saved, shallow_bootstrapped True, loud log).
- Empty fetch → RuntimeError, NO cache file written (C3).
- allow=True + data_client None → the byte-identical FileNotFoundError (C2).
- C5 run-2 pin: cache present / seed absent / ledger absent → clean boot, ledger
  accrual skipped loudly, no raise.
- 5m-model (bar_size "5m") seedless → exact FileNotFoundError (guard pin).
- live_trader wiring: 1h config passes allow=True; 5m config passes False; ES01B
  (key removed) constructs the 5m manager, watchdog anchors 5m/15-min, trailing
  reads the 5m frame (existing presence pins).
- The 7 sanctioned evolutions.

## Verification
- Full fast suite green — NOTE: baseline at HEAD is 6 FAILED / rest passed; post-ticket
  it must be ZERO failed (the repair is part of this ticket's green).
- C6 BLOCKING: HS14B ledger parity gate (`setup --disable-trailing`, 2200/336) →
  PARITY: PASS before commit.
