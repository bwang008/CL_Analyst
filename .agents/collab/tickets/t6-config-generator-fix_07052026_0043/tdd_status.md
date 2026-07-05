# TDD Status — t6-config-generator-fix_07052026_0043

## PHASE: Red
**TDD-Tester** | 2026-07-05 | HEAD 2a8311f (development, no worktree)

## Deliverables
1. **NEW Strict-Lock test file** `tests/test_config_generator_symbols.py` (85 collected with T1 file; 56 in this file alone) — classes:
   - `TestDeriveDatasetTag` — absolute audit-§6 table (15 params incl. empty / symbol-only / empty-remainder / double-prefix / bk_-precedence / case-preserving edges) + differential vs a FROZEN in-test transcription of `gcp/vm_e2e_pipeline.py:655-661` over 26 real-shaped filenames × 8 symbols (CL/ES/ZC/GC/NG/SI/ZS/NQ) — zero mismatches required (reviewer's 268-case method).
   - `TestTagSingleSource` — identity pins `agent.generate_ensemble_artifacts.derive_dataset_tag is src.core.dataset_tag.derive_dataset_tag` (same for `gcp.vm_e2e_pipeline`) + inline `bk_(.+)$` literal count == 0 in both consumer files (de-dup fence).
   - `TestGeneratorSymbolPropagation` — tmp_path mini-repo fixtures (manifest + opt-results + top_pairs + sweep tree; `subprocess.run` patched; `monkeypatch.chdir`): CL legacy `bk_` byte-identical-except-D2-keys (frozen HEAD emission transcription, oracle-verified byte-faithful vs HEAD in scratchpad); CL v2 `CL_HourSet_14B` → corrected `E2E_HourSet_14B_*` prefixes + fixture-tree existence (D1); ES manifest → execution_symbol/models.*.symbol "ES" + resolver round-trip called in-test; missing `baseline.symbol` → ValueError FATAL; unknown `"XX"` → ValueError via get_instrument; tamper fixture (`brain_symbol: "ES"` doctored into the base config — the one mismatch that survives symbol stamping) → generator RAISES via post-emission self-check.
   - `TestES01BPatchedConfig` — shipped config resolves ES/ES/CME (multiplier 50, tick 0.25); §2 10-field values pinned exactly; all 4 referenced artifacts exist on disk (target paths verified present at HEAD); untouched-field sentinels (passes at Red by design).
   - `TestVmE2eEnsembleCfg` — ensemble_cfg emission stamps execution_symbol + models.*.symbol from the run `symbol`. Emission is INLINE in heavy `run_pipeline` and blueprint mandates a one-liner (no extraction), so the narrowest failing-today seam is a source pin on `inspect.getsource(run_pipeline)` (regexes accept dict-literal or assignment stamping forms).
   - `TestCosmetics` — ONLY the audit-§5f enumerated items: `_backup_cache_to_repo` per-symbol names (CL byte-identical pin PASSES at Red; ES namespaced FAILS); smoke cadence regex (`warm_start_cache_ZC_1h.parquet` et al FAIL; CL/legacy names pinned PASS); m1 `[PNL]` display (CL 65,000→65.00 pinned PASS; ES 300,600→6,012.00 FAILS); m3 strings (dry-run line CL byte-identical pin + ES-symbol FAIL; banner "(CL Only)" pin + ES FAIL; qualify_contract sync+async actual-symbol FAIL with CL pin; cli `--quantity` help symbol-neutral FAIL with "CL Analyst" title LEAVE pin). Account-summary dict KEYS deliberately provided cl_*-only (m2 rename deferred — premature rename fails loudly). Warmup `entry_crossed` NOT touched (ticket-excluded).
2. **THE ONE Strict-Lock evolution** in `tests/test_instrument_context.py` (exactly the two self-documented "until T6" pins; nothing else changed; T6 evolution note added to the module docstring citing this ticket ID):
   - `:273` `test_es01b_shipped_config_raises_intended_failure` → `test_es01b_shipped_config_resolves_as_es` (resolves ES/ES/CME; models.*.symbol == "ES"; model_paths + predictions_paths exist on disk).
   - `:305` `test_all_shipped_configs_resolve_except_es01b` → `test_all_shipped_configs_resolve` (`intended_failures` now the EMPTY set).

## Red proof (2026-07-05)
`conda run -n trader python -m pytest tests/test_config_generator_symbols.py tests/test_instrument_context.py -q --tb=no --continue-on-collection-errors`
```
tests\test_config_generator_symbols.py FFFFFFFFFFFFFFFFFFFFFFFFFFFF.FF.F [ 38%]
...FFFF..F.F.F.FFF                                                       [ 60%]
tests\test_instrument_context.py ..........................F..F....      [100%]
================== 43 failed, 42 passed, 1 warning in 2.17s ===================
```
- New file: 41 FAIL (missing `src.core.dataset_tag`, un-stamped symbols, dead `E2E_CL_*` prefixes, DID-NOT-RAISE fail-fasts, unpatched ES01B, missing vm_e2e stamps, cosmetics) + 15 CL-pin PASSES (byte-identity fences, intended to pass at Red AND Green).
- T1 file: exactly the two flipped pins FAIL (`test_es01b_shipped_config_resolves_as_es`, `test_all_shipped_configs_resolve` — both via the shipped config's model-symbol-mismatch ValueError); all 33 other T1 tests stay GREEN.
- Failure-reason spot checks: legacy-CL fails ONLY on the absent D2 key (`assert None == 'CL'`); v2-CL fails on `E2E_CL_HourSet_14B_* != E2E_HourSet_14B_*`; ES fails on `execution_symbol 'CL' != 'ES'`; tag tests fail `ModuleNotFoundError: src.core.dataset_tag`.
- Oracle verification (scratchpad, not committed): frozen emission transcription is byte-faithful vs HEAD for both legacy bk_ and v2 fixtures (`serialization-pin=True head-shape-match=True` both) — the byte-identity comparisons cannot false-fail a correct implementation.
- Neighbors: `tests/test_instrument_master_live_fields.py tests/test_symbol_data_paths.py` → **125 passed**.
- ES01B patch targets verified present on disk at HEAD (both `E2E_HourSet_01B_{long,short}_logloss/final_model.pkl` + `batch_20260704_0701_ES_01B_SCOUT/.../ES01B_Sharpe_E03_predictions.csv`) — the flipped pins are green-able by the Coder's surgical patch alone.

## Notes for the Coder
- Do NOT modify `tests/test_config_generator_symbols.py` or `tests/test_instrument_context.py` (Strict-Lock).
- `derive_dataset_tag` must be importable at MODULE level of both consumers (identity pins) and the inline `bk_(.+)$` blocks removed from both.
- The generator self-check must let the resolver's ValueError propagate (tamper test pins ValueError, not SystemExit).
- m1: the `[PNL]` multiplier seam must resolve structurally for `object.__new__` stubs that set only `_execution_symbol` (follow the `_tick_size`/`_brain_instrument` registry-fallback property precedent).
- C4 gate (HS14B ledger parity PASS) remains a pre-commit condition per blueprint — not covered by these unit tests.

---

# PHASE: Green
**TDD-Coder** | 2026-07-05 | branch development (no worktree, uncommitted per manager instruction)

## Implementation delivered (per blueprint Target Files)
1. **NEW `src/core/dataset_tag.py`** — `derive_dataset_tag(basename, symbol)`, byte-for-byte the vm_e2e_pipeline HEAD :655-661 logic (bk_ precedence, case-insensitive prefix match with case-preserving slice, passthrough). Stdlib leaf. Differential + absolute-table pins green.
2. **`agent/generate_ensemble_artifacts.py`** — module-level import of the shared helper (+ `sys.path` root bootstrap so `python agent/generate_ensemble_artifacts.py` script-mode runs on the VM resolve `src.*`; the file had NO bootstrap and module-level src imports would otherwise break the vm_post_optimize.sh invocation); inline `bk_(.+)$` block REMOVED (locked zero-count pin green); `manifest.baseline.symbol` REQUIRED (ValueError FATAL when missing/empty; `get_instrument()` fail-fast for unknown symbols); `cfg["execution_symbol"]` stamped after the deep copy (in-place overwrite — CL byte-identical); D2 `models.{long,short}.symbol` stamped unconditionally; post-emission `resolve_instrument_context(cfg)` self-check RAISES before json.dump (tamper pin green); model_path existence = print WARN only.
3. **`gcp/vm_e2e_pipeline.py`** — both duplicated tag blocks (:652-661, :733-740) replaced by the shared helper (module-level import → identity pins green; both inline `import re` copies removed, zero `bk_(.+)$` remains); ensemble_cfg emission stamps `execution_symbol = symbol` + `"symbol": symbol` in both models dict literals (source pins green; value-preserving for CL runs).
4. **`configs/strategies/ES01B_Sharpe_E03_07042026.json`** — surgical patch EXACTLY per audit §2 10-field table (execution_symbol ES; both experiment_ids/model_paths → `E2E_HourSet_01B_*`; both predictions_paths → `batch_20260704_0701_ES_01B_SCOUT`; `"symbol": "ES"` added per side; NO brain_symbol; all other bytes untouched — sentinel pins green, resolver round-trip ES/ES/CME green, all 4 artifacts exist on disk).
5. **Cosmetics (audit §5f only)** — `data_manager._backup_cache_to_repo` derives backup names from `self.cache_path.stem` / `Path(self.roll_metadata_path).stem.lstrip('.')` (CL literals byte-identical; ES → `warm_start_cache_ES_*`/`roll_metadata_ES_*`); `smoke_test_pipeline._expected_cache_timestep` accepts `warm_start_cache_{SYM}[.parquet]` → 5m and `warm_start_cache_{SYM}_{N}{mh}` → N m/h (legacy names unchanged, non-cache names still None); m1 `[PNL]` division → NEW read-only `LiveTrader._execution_instrument` property (InstrumentContext-preferred, registry fallback via `_execution_symbol` — exact `_tick_size` precedent) so `avg_cost / multiplier` (CL 65,000→65.00 pinned; ES 300,600→6,012.00); m3 dry-run line names `self._execution_symbol` (CL byte-identical); account banner text symbol-derived with dict KEYS left cl_* (m2 deferred); qualify_contract(+async) errors name `contract.symbol`; cli `--quantity` help symbol-neutral, "CL Analyst" title untouched.
6. **Ticket bookkeeping** — C1 census amendment (site #8 strategy_optimizer `_opt_/_hybrid_` writes; row #3 correction; residual→T8), C2 flag (generator `defaults`-less manifest silently uses the CL base config — symbol-benign post-T6, T8 candidate), C3 (batch_20260630_2232_SCOUT_14B_FAIL full local name) appended to ticket_status.md.

## One deviation beyond the enumerated diff (justified)
- `LiveTrader._print_account_summary`: the symbol read (`sym = self._execution_symbol`) had to live INSIDE the existing try/except — prior-ticket Strict-Lock `tests/test_account_summary.py::test_handles_exception_gracefully` builds an `object.__new__` stub with no `_execution_symbol`/`exec_client` and requires graceful skip. First full-suite run caught it (1 failure); fixed by moving the read into the try; suite re-run fully green. No behavior change for real traders (attribute always set by `__init__`).
- `agent/generate_ensemble_artifacts.py` PROJECT_ROOT sys.path bootstrap (see item 2): required so the NEW module-level `src.*` imports don't break VM script-mode invocation; matches the established convention of 18 sibling agent/ scripts.

## Green proof (2026-07-05)
1. `conda run -n trader python -m pytest tests/test_config_generator_symbols.py tests/test_instrument_context.py -v --tb=short` → **85 passed** (51 new-file + all 34 T1 incl. the two flipped pins).
2. Neighbors `tests/test_instrument_master_live_fields.py tests/test_symbol_data_paths.py tests/test_session_watchdog_rollover.py tests/test_macro_vol_parameterization.py -q` → **280 passed**.
3. Full fast suite `tests/ -q -m "not slow"` → **1335 passed, 0 failed** (1284 baseline incl. the 2 re-greened pins + 51 new).

## Outstanding for the Manager
- C4 BLOCKING pre-commit gate: HS14B ledger parity (`setup --disable-trailing`, 2200/336) → PARITY: PASS — not covered by unit tests; must run before commit.
- No test files modified; both Strict-Lock contracts untouched.
