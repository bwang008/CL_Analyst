# TDD Result — seedless-5m-live-stream_07052026_0546

**Outcome: GREEN + PARITY PASS — ticket complete. Reviewer verdict: APPROVE (C1-C6).
USER-DECIDED design: the 5m seed requirement follows what the model consumes.**

- Red: 19 new tests (17 failing / 2 pins) + 7 sanctioned pin evolutions (6 of which
  REPAIRED the pins broken at HEAD by 336d29f's MES/cid-2000 config flip — manager's
  miss, owned); manager-verified baseline: exactly 1 failed / 1380 passed.
- Green: **1400 passed, 0 failed** (manager-verified independently) — suite fully
  healed.
- C6 blocking parity gate: **PARITY: PASS**, exit 0 — ninth consecutive.

## Behavior shipped
- Seed/cache exists → byte-identical (CL untouched, deep window + ledger accrual).
- Seedless + hourly model (bar_size 1h/2h/4h) → ONE "5 D" IBKR fetch bootstraps the
  5m window; RuntimeError on empty BEFORE any cache write; loud 3-surface disclosure
  (SHALLOW 5M BOOTSTRAP log + warm-start banner + Telegram Mode stamp); first-run
  save_cache() makes run 2+ a normal cache warm-start; 5m master-ledger accrual
  skipped loudly while seedless (no consumers; cache accrues all bars).
- Seedless + 5m model → the exact FileNotFoundError survives (dormant NaN-feature
  guard for future 5m models).
- ES01B: `enable_5m_stream: false` REMOVED — ES now runs the identical default path
  as CL (5m stream, 5m trailing granularity, 5m/15-min watchdog). The flag remains an
  explicit opt-out only.
- Docs: 6 locations updated — new symbols need NO 5m files; plus a new sentinel-pin
  gate (d) in /add-remove-fleet-model (the 336d29f lesson: any shipped-config edit
  must evolve its pins in the same change).

## Files changed
- `src/live_execution/data_manager.py` — allow_shallow_bootstrap param,
  _shallow_bootstrap_from_ibkr, ledger gate.
- `src/live_execution/live_trader.py` — bar-size-keyed wiring, banner + Telegram stamp.
- `configs/strategies/ES01B_Sharpe_E03_07042026.json` — flag line removed (C1
  same-commit).
- `tests/test_shallow_5m_bootstrap.py` — NEW, 19 tests (Strict-Lock).
- Pin evolutions in test_hourly_only_equity_session.py / test_instrument_context.py /
  test_config_generator_symbols.py (7, sanctioned).
- Docs: build-symbol-pipeline.md, run-live.md, add-remove-fleet-model.md, grab-data.md,
  headless-deployment.md, deploy/systemd/README.md.

## Notes
- `src/live_execution/utils/telegram_alert.py` carries another session's uncommitted
  diff — EXCLUDED from this commit.
- Pre-existing order-dependent test leak (closure-local _SymbolPrefixFilter on the
  shared LiveTrader logger, live_trader.py ~:554) — micro-ticket candidate, untouched.
- Operator action: RESTART THE FLEET to pick this up; expect the SHALLOW 5M BOOTSTRAP
  banner on ES's first boot, then normal cache warm-starts.
