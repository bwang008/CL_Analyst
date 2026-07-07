# Ticket Resolution Blueprint — live-seed-1h-loud-materialize_07062026_2124
**Ticket Directory:** `.agents/collab/tickets/live-seed-1h-loud-materialize_07062026_2124/`

## Bug Summary
The live engine resolves the per-symbol 1h seed to `<SYM>_raw_1h.parquet`
(`derive_data_paths`, `src/live_execution/data_manager.py:105`) for every symbol,
but the symbol-standup pipeline only *produces* `<SYM>_raw.parquet` (the hourly
execution parquet). The two files are byte-identical; they are bridged only by a
**manual** `Copy-Item <SYM>_raw.parquet <SYM>_raw_1h.parquet` step
(build-symbol-pipeline Phase 1 step 7). That step was skipped for SI/NQ/ZC/ZS, so
`SI01B` crash-looped at live start with
`FileNotFoundError: Neither 1H cache nor seed file found for SI`
(`live_trader.py:525`).

**Root cause:** a naming mismatch between the batch/execution convention
(`<SYM>_raw.parquet`, also used by every manifest's `execution_data_path`) and the
live-seed convention (`<SYM>_raw_1h.parquet`), bridged by an easily-forgotten
manual copy.

**Design constraint (USER RULING — do NOT violate):** NO silent read-through
fallback. A silent substitution of a "default" file into a production data path is
a catastrophic-risk anti-pattern. The fix must be either a **loud, idempotent
auto-materialization** (copy + alert) or a **loud hard failure** — never a quiet
guess. This preserves the `no-silent-null-defaults` rule.

## Target Files
- `src/live_execution/data_manager.py` — add a shared, idempotent
  `materialize_1h_seed(symbol)` (or similar) helper; keep `derive_data_paths` as
  the naming authority (it still returns the `<SYM>_raw_1h.parquet` path).
- `src/live_execution/fleet_runner.py` — `_check_requirement` / preflight
  (~lines 321-380): call the materialize helper before declaring the seed missing.
- `src/live_execution/live_trader.py` — startup 1h seed check (~lines 499-534):
  call the same helper before the hard raise.
- `tests/test_symbol_data_paths.py` — CL byte-identical guard (regression).
- New: `tests/test_seed_1h_materialize.py` — behavior tests (materialize + loud + fail-fast).
- Docs: build-symbol-pipeline Phase 1 step 7, add-remove-fleet-model step 2 —
  note the safety net exists but staging at standup is still required.

## Required Changes
1. **Loud idempotent materialization.** When the resolved seed
   `<SYM>_raw_1h.parquet` is absent, the 1h cache is absent, AND
   `<SYM>_raw.parquet` exists in the same `processed/` dir: **copy**
   `<SYM>_raw.parquet` → `<SYM>_raw_1h.parquet` so the canonical seed physically
   exists, THEN proceed. Emit a prominent WARNING banner and a Telegram alert
   (reuse the existing 5m shallow-bootstrap notification path) stating: seed was
   auto-materialized, the standup step (build-symbol-pipeline Phase 1 step 7) was
   skipped for `<SYM>`, and it must be back-filled. This is a heal-and-announce,
   not a silent fallback.
2. **No silent read-through.** Never point the DataManager at `<SYM>_raw.parquet`
   in place of the seed; the seed file must exist on disk after materialization so
   cache-rebuild and provenance stay consistent.
3. **Fail-fast preserved.** If NONE of {`_1h` seed, 1h cache, `<SYM>_raw.parquet`}
   exists → keep the hard `FileNotFoundError` with the existing actionable message.
4. **Freshness still enforced.** After materialization, the existing
   `REQUIRED_1H_BARS` (4320 in-window) and `max_gap_days` staleness checks must
   still run, so a stale copy is caught loudly (not silently traded on).
5. **CL byte-identical.** CL owns `CL_raw_1h.parquet`, so the materialize branch
   must never trigger for CL. Add/keep a regression test asserting CL seed
   resolution is unchanged.
6. **Single helper, both paths.** Put the copy/alert logic in ONE helper in
   `data_manager.py` so the fleet preflight and single-instance startup both heal
   identically (coordinates with ticket `cli-seed-preflight_07062026_2124`).

## Dependencies / Coordination
- Shares the materialize helper with `cli-seed-preflight_07062026_2124` — land the
  helper here; that ticket wires the CLI to it.
