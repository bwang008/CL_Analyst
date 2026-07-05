# TDD Status — t8-workflow-doc-gates_07052026_0403

**Role:** Doc-Writer (TDD-Coder, doc-only variant) | **Date:** 2026-07-05 | **Branch:** development @ `3738516`

## PHASE: Docs-applied

All 13 blueprint edit items applied (audit §3-§5 base texts, amended by R1-R5), plus the two
audit §4 NOTE-only items (#6 generate-data.md, #7 sweep-ensembles.md) incorporated by the
blueprint's "apply audit §3-§5 EXACTLY" header. No source code or test file touched. Nothing
committed (manager commits).

### Files edited (18)
| File | Edit |
|------|------|
| `.agents/workflows/build-symbol-pipeline.md` | Phase 0 full 17-field registry spec + GATE 0; Phase 1 step 7 (1h seed + hourly-only ruling); Phase 5 C1/C2 replacing line-90 softness; Phase 6 CONFIG VALIDATION GATE script (R3 zero-configs=FAIL, R4 path bootstrap, R1 dual fixture expectations); Phase 7 report line; +3 checklist boxes; +5 key-files rows |
| `.agents/workflows/run-cloud-batch.md` | baseline.symbol REQUIRED + C2 `defaults` note; both PS invocations → `& .\gcp\run_sweep_batch.ps1`; new §5 config gate step; configs/ output-tree annotation |
| `.agents/workflows/post-optimize.md` | C1 warning box on Option B; post-download config gate; PS prefix fix |
| `.agents/workflows/generate-trade-configs.md` | Step 3 ES01B-defect warning + explicit symbol-stamping sub-steps + single-config gate before Step 4 |
| `.agents/workflows/run-live.md` | Preflight section (resolver one-liner, seed, macro, enable_5m_stream); canonical `-m src.live_execution.cli` entry |
| `.agents/workflows/grab-data.md` | Hourly-only ruling banner; ZC/ZS/SI rows; Step 7 → Phase 0 registry gate pointer |
| `.agents/workflows/livetest.md` | Known Differences §1 rewritten per R2 (penny grid vs tick grid, CL-only equality, ≤½-tick non-CL gap); resolver precondition in Adapting section |
| `.agents/workflows/smoketest.md` | Per-symbol `warm_start_cache_{SYM}[_1h].parquet` names + CL legacy-exception note |
| `.agents/workflows/run-cloud-experiment.md` | Legacy-v1 banner → /run-cloud-batch; single-config gate required before configs/strategies/ |
| `.agents/workflows/run-vector-cloud-batch.md` | R5 deprecation banner: retired hourset09 manifests + legacy `defaults` schema (NOT the still-live `-SweepMode "frictionless"`) |
| `.agents/workflows/validate-parity.md` | CL-fixtured scope note (session/watchdog/front-month/seed not exercised) |
| `.agents/workflows/validate-ledger-parity.md` | Same scope note |
| `.agents/workflows/generate-data.md` | NOTE: DataMap symbol must be fully registered; MacroFeatureEngine hard-raises (T4) |
| `.agents/workflows/sweep-ensembles.md` | NOTE: --base-config field propagation warning |
| `.agent/workflows/run-cloud-experiment.md` | Deprecation banner → `.agents/` twin (after frontmatter; content untouched) |
| `.agent/workflows/run-experiment.md` | Same banner |
| `docs/headless-deployment.md` | Fleet per-config prerequisites (resolver fail-fast, per-symbol seed/macro, enable_5m_stream); Cloud Migration §3 per-symbol artifacts |
| `deploy/systemd/README.md` | Same fleet prerequisites section |

### Verification
1. **Gate dual-run** (script transcribed byte-identically from the doc, `conda run -n trader`, repo root):
   - `reports\batch_runs\batch_20260704_0701_ES_01B_SCOUT` → **exit 1**, 8/8 configs fail with the
     resolver error (`execution_symbol 'CL' ... does not match model symbol 'ES'`) — R1 negative
     fixture confirmed.
   - Temp dir (`configs/ES01B_Sharpe_E03_07042026.json` copy + `{"baseline": {"symbol": "ES"}}`
     manifest stub) → **`CONFIG GATE: PASS`, exit 0** — R1 positive fixture confirmed.
2. **Fast suite:** `conda run -n trader python -m pytest tests/ -q -m "not slow"` →
   **1381 passed**, 259 warnings in 150.35s (baseline held).
3. **Diff proofread:** `git diff --stat` shows only the 18 documentation files above + this ticket
   folder (the `data/predictions/*.csv` modifications pre-date this ticket and belong to other
   sessions, per spawn brief).

### Facts verified beyond the review's list (read-only checks)
- `configurable_strategy.py` full path = `src/live_execution/strategies/configurable_strategy.py`
  (`round(x, 2)` confirmed at :561-565) — used in the livetest.md R2 text.
- `SYMBOL_MAP` ZC/ZS/SI = `ZC.v.0`/`ZS.v.0`/`SI.v.0` (`databento_data_builder.py:65-67`); registry
  names/exchanges Corn/CBOT, Soybeans/CBOT, Silver/COMEX — used in the grab-data.md table.
- `derive_data_paths` cache names (`data_manager.py:91-99`): CL legacy `warm_start_cache[_1h].parquet`,
  per-symbol `warm_start_cache_{SYM}[_1h].parquet` — used in smoketest.md.
- `cli.py` accepts `--config`/`--dry-run` (module docstring shows the exact canonical invocation).
