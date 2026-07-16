# TDD Result — fred-macro-vol-column-missing_07152026_1836

**Outcome: GREEN.** Full fast suite `conda run -n trader python -m pytest tests/ -m "not slow"` →
**2351 passed, 1 skipped, 0 failed** (Red baseline was 2343 passed + 8 failing new tests; the 8 flipped green, zero regressions).

## What was fixed
Silent partial write in `scripts/download_macro_data.py`: a failed/empty FRED series was dropped and the
partial file written anyway, overwriting a known-good file. The live trader corrupted its own macro input
this way (GC lost GVZ, SI lost FED_FUNDS on 2026-07-15). Fix makes the producer fail loudly and validate
its schema before writing. Consumer-side hard-raise at `macro_features.py:588` left untouched (it is correct).

## Files changed
- `scripts/download_macro_data.py` — **+97 / −45** (F3 is a net deletion; F1/F2 add retry + schema-guard + atomic write)
- `tests/test_download_macro_data_fred.py` — **NEW**, 11 tests (8 fix-target + 3 green guardrails)
- `src/live_execution/live_trader.py` — **NOT touched** (F4 deferred to follow-up per operator).

## Implementation summary
- **F1 `download_fred_data`** — bounded 3-attempt retry (`0.5*attempt` backoff) around `fred.get_series`;
  empty/None response now counts as a failed attempt (was the `:105-107` silent warn+continue); any series
  still failing after retries → `raise RuntimeError` naming the series id(s). No partial dict escapes. Global-
  attribute `download_fred_data.instrument` hack removed; instrument passed explicitly.
- **F2 `save_fred_data`** — validates assembled frame against `_FRED_BASE_COLS | {vol_label_for(instrument)}`
  (both imported top-level from `src.features.macro_features` — import is cycle-free; macro_features imports
  this script only lazily). Missing column → `ValueError` naming it, **before any write**, so a pre-existing
  good file is left byte-identical. Atomic write via `to_csv(tmp)` + `os.replace`, with a `finally` that
  unlinks leftover temp on success or failure. `instrument is None` → validate base cols only (no crash).
- **F3 `main()` FRED branch** — deleted the 15-line inline writer, routed through
  `download_fred_data(api_key, instrument=instrument)` + `save_fred_data(fred_data, instrument=instrument)`.
  COT branch untouched (its dead-writer twin is follow-up ticket #1).

## Verification
- Target file: 11/11 green.
- Consumers unbroken: `test_cot_adapters.py` (12) + `test_macro_vol_parameterization.py` (83) green.
- Full fast suite: 2351 passed / 1 skipped / 0 failed. Diff confined to the two files above; `live_trader.py`
  shows no diff.

## Live-safety (from blueprint, re-confirmed by coder)
The F1 `RuntimeError` propagates through `refresh_if_stale`'s `except…raise`: at the heartbeat it hits the
pre-existing live catch-all (`live_trader.py:5516-5517`) — log + continue, **known-good file untouched**;
at startup it aborts the child exactly like the existing `FRED_API_KEY`/COT raises. Either path is strictly
better than today's silent corruption. Happy-path output unchanged (same columns + ffill via `save_fred_data`).

## Status of the ticket's parts
- F1 / F2 / F3 — **DONE, committed-ready** (uncommitted in working tree; commit + deploy operator-gated).
- F5 (regenerate GC/SI) — already DONE + live-verified 2026-07-15 (see blueprint / ticket_status).
- F4 (live_trader alerting) — **DEFERRED** by operator → `followups.md` FU-1.
- Follow-ups FU-2 (COT twin), FU-3 (base-column silent skips), FU-4 (fleet preflight header check) — filed in `followups.md`.

## NOT YET DONE (operator-gated)
- **Commit** — changes are uncommitted on `development`. Per project convention (sequential tickets commit
  per task on development) this is ready to commit; left to operator.
- **Canary before pipeline change** — `download_macro_data.py` feeds training/backtest data. Happy-path output
  is unchanged (byte-identical for a fully-successful download), so risk is low, but per the project rule a
  canary is advisable before any cloud scout/prod run that regenerates macro data from this tree.
