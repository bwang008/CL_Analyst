# TDD Status — seedless-5m-live-stream_07052026_0546

## PHASE: Red
**TDD-Tester** | 2026-07-05 06:34 PT | Base: HEAD `f165b9d` (branch `development`, no worktree — worked in place, no running process disturbed)

## Deliverables

### 1. New failing test file (Strict-Lock)
`tests/test_shallow_5m_bootstrap.py` — 19 tests: 17 FAIL at Red (missing implementation), 2 deliberate pins pass at Red AND Green (documented per-test).

**TestShallowBootstrapDataManager** (tmp_path-isolated paths, mocked data client, `ib_bars_to_dataframe`-shaped frames):
| Test | Pins | Red failure mode |
|---|---|---|
| `test_cl_seed_branch_default_param_never_bootstraps` | CL seed branch byte-identical; `allow_shallow_bootstrap` DEFAULTS False (signature pin); `shallow_bootstrapped is False`; bootstrap mock never called; no SHALLOW log | AttributeError: no `allow_shallow_bootstrap` |
| `test_cl_cache_present_allow_true_is_dead_code` | cache branch wins over allow=True (presence-conditional); same bar count; no bootstrap call | TypeError: unexpected kwarg |
| `test_happy_path_bootstraps_once_saves_cache_loudly` | seedless+cacheless+allow=True → initialize() succeeds, window populated, `shallow_bootstrapped is True`, cache WRITTEN, exactly ONE `duration_str="5 D"` fetch with `continuous=True`/`bar_size="5 mins"`/`what_to_show="TRADES"`/`use_rth=False` (C3), "SHALLOW 5M BOOTSTRAP" caplog | TypeError: unexpected kwarg |
| `test_empty_fetch_raises_before_any_cache_write` | empty fetch → RuntimeError; NO cache file after (C3 raise-before-save) | TypeError: unexpected kwarg |
| `test_allow_true_without_data_client_keeps_byte_identical_raise` | allow=True + data_client=None → BYTE-IDENTICAL FileNotFoundError (message transcribed from data_manager.py:303-308) (C2) | TypeError: unexpected kwarg |
| `test_default_allow_false_seedless_raise_byte_identical_guard_pin` | default construction → byte-identical raise, client never consulted (dormant 5m-model NaN guard) | AttributeError: no `allow_shallow_bootstrap` |
| `test_run2_cache_present_seed_ledger_absent_boots_cleanly` | **C5 — THE test**: cache PRESENT / seed ABSENT / ledger ABSENT / allow=True → initialize() boots CLEANLY, no bootstrap, ledger accrual skipped LOUDLY, no ledger file founded | TypeError: unexpected kwarg |
| `test_seedless_gate_never_calls_load_full_seed` | seedless gate: `_update_training_ledger` returns without `_load_full_seed()`/`_save_ledger()`, loud skip log | TypeError: unexpected kwarg |
| `test_cl_with_seed_still_accrues_ledger_spy_pin` | seed present + allow=True → gate predicate False → `_load_full_seed` + `_save_ledger` called once each (CL accrual byte-identity) | TypeError: unexpected kwarg |

**TestLiveTraderShallowWiring** (T5/T7 seams REUSED by import from `tests/test_hourly_only_equity_session.py` — tests/ is a package; Strict-Lock file untouched by the import):
| Test | Pins | Red failure mode |
|---|---|---|
| `test_hourly_config_wires_allow_shallow_bootstrap_true[1h/2h/4h]` | 5m DM constructed with `allow_shallow_bootstrap=True`; 1h DM gets NO shallow wiring | AssertionError: kwarg absent (None is not True) ×3 |
| `test_5m_config_wires_allow_false_nan_guard` | bar_size "5m" → explicit `allow_shallow_bootstrap=False` | AssertionError: None is not False |
| `test_es01b_shipped_config_constructs_5m_manager_by_default` | shipped ES01B (key removed): `_enable_5m_stream is True`, `data_manager_5m` not None, 2 DMs, 5m call = brain ES / execution MES / allow=True | AssertionError: flag False (key still present — green only after Coder's same-commit config edit, C1) |
| `test_es01b_watchdog_anchors_5m_15min` | shipped-config flag resolves True → watchdog anchors `_last_bar_time_5m`: 16 min stale during equity OPEN (Tue 12:00 CT) → True; 10 min → False | AssertionError: flag False at Red |
| `test_es01b_trailing_5m_frame_drives_when_present_pin` | **PIN (passes Red+Green)**: MES variant of the existing presence pin — 5m frame drives extremes/SL on the 0.25 grid | passes (by design) |
| `test_warm_start_banner_when_shallow` | `_warm_start` latches `_shallow_5m_bootstrap=True` + "SHALLOW 5M" banner (narrowest disclosure seam) | AssertionError: state attr never set |
| `test_warm_start_no_banner_when_not_shallow` | anti-noise control: state False, NO banner | AssertionError: state attr never set |
| `test_t7_flag_false_fixture_still_opts_out` | **PIN (passes Red+Green)**: T7 synthetic flag-false fixture (reused via import) still constructs hourly-only — opt-out valid | passes (by design) |

### 2. The 7 sanctioned pin evolutions (all cite the ticket ID in docstrings)
1. `tests/test_hourly_only_equity_session.py::TestES01BFlagPatch::test_es01b_carries_enable_5m_stream_false` → **RENAMED** `test_es01b_no_longer_carries_enable_5m_stream_key` (C4 name honesty): `live.get("enable_5m_stream") is False` → `"enable_5m_stream" not in live`. **FAILS at Red** (key still present) — green only with the Coder's config edit (C1 same-commit).
2. `...::test_es01b_still_resolves_es_es_cme`: `ctx.execution_symbol == "ES"` → `== "MES"`; `brain_symbol == "ES"` kept; exchange CME / tick 0.25 kept. Was FAILING at HEAD (336d29f) → now PASSES.
3. `...::test_es01b_t6_sentinel_fields_unchanged`: `cfg["execution_symbol"] == "ES"` → `== "MES"`; `client_id == 1010` → `== 2000`; all other sentinels (thresholds .53/.56, experiment ids, marketable_limit ×2, holdout 6, conflict_resolution, `"brain_symbol" not in cfg`, bar_size, nickname) unchanged. Was FAILING → now PASSES.
4. `tests/test_instrument_context.py::TestShippedConfigs::test_es01b_shipped_config_resolves_as_es`: `ctx.execution_symbol == "ES"` → `== "MES"`; brain ES / exchange CME / models handshake / artifact-existence asserts unchanged. Was FAILING → now PASSES.
5. `tests/test_config_generator_symbols.py::TestES01BPatchedConfig::test_patched_fields_match_audit_table`: `cfg["execution_symbol"] == "ES"` → `== "MES"`; models pins unchanged. Was FAILING → now PASSES.
6. `...::test_resolves_as_es_es_cme`: execution `"ES"` → `"MES"`; **`multiplier == 50` → `== 5`** (the execution instrument is now the MICRO — MES registry value; consequential to the same 336d29f repair, would have failed one line later otherwise); brain ES / CME / tick 0.25 kept. Was FAILING → now PASSES.
7. `...::test_untouched_fields_pinned`: `client_id == 1010` → `== 2000`; all other sentinels unchanged. Was FAILING → now PASSES.

Nothing else changed in those three files (headers untouched; no other test bodies touched).

### 3. Red proof (ticket verification command)
`conda run -n trader python -m pytest tests/test_shallow_5m_bootstrap.py tests/test_hourly_only_equity_session.py tests/test_instrument_context.py tests/test_config_generator_symbols.py -v --tb=short --continue-on-collection-errors`

```
================== 22 failed, 128 passed, 1 warning in 5.36s ==================
```
- 17 = new file (7× TypeError "unexpected keyword argument 'allow_shallow_bootstrap'", 2× AttributeError on the signature-default pin, 4× wiring-kwarg-absent, 2× ES01B flag-still-false, 2× `_shallow_5m_bootstrap` never set).
- 1 = the renamed key-absent pin (`enable_5m_stream` still in live_config — Coder removes it, C1).
- 6 former MES-pin failures now PASS (336d29f repair verified).
- 4 = `test_config_generator_symbols.py::TestCosmetics` (`test_pnl_display_division_cl_pinned`, `test_pnl_display_division_es_uses_instrument_multiplier`, `test_dry_run_log_cl_byte_identical`, `test_dry_run_log_es_names_instance_symbol`) — **PRE-EXISTING order-dependent leakage, NOT caused by this ticket** (see finding below).

T5 fence: `conda run -n trader python -m pytest tests/test_session_watchdog_rollover.py -q` → **72 passed**.

## FINDING (for Manager/Coder — outside my sanction to fix)
**Pre-existing cross-file test-isolation gap, exposed by the ticket's 4-file CLI order:** any completed `LiveTrader.__init__` installs a `_SymbolPrefixFilter` on the shared "LiveTrader" logger (`live_trader.py:554-556`) that mutates every subsequent record (`record.msg = "[SYM] " + msg`) and is NEVER removed; the dedup at :554 is broken across constructions because the filter class is defined closure-locally per `__init__` (isinstance never matches older instances). `TestCosmetics` pins `r.getMessage().startswith("[PNL]")` / `startswith("DRY RUN")` on `__new__` stubs, so it breaks whenever ANY full construction ran earlier in the same process.
- **Proven pre-existing with two tests this ticket never touched:** `pytest tests/test_hourly_only_equity_session.py::TestEnable5mStreamFlag::test_default_true_cl_construction_byte_identical tests/test_config_generator_symbols.py::TestCosmetics::test_pnl_display_division_cl_pinned` → 1 failed (the Cosmetics pin) at the current tree; also reproduces with the 3 pre-existing files alone in the ticket's CLI order (hourly_only before config_generator).
- `tests/test_config_generator_symbols.py` alone: **51 passed** (all green).
- **Full fast suite is UNAFFECTED**: in alphabetical collection order `test_config_generator_symbols.py` (c…) runs BEFORE the construction-heavy files (h…, s…) — verified: `pytest tests/test_config_generator_symbols.py tests/test_shallow_5m_bootstrap.py` → TestCosmetics green, exactly my 17 Red failures. The new file cannot contaminate the full-suite baseline.
- Recommended (NOT done — Strict-Lock/scope): micro-ticket to hoist `_SymbolPrefixFilter` to module level (making the :554 dedup actually work) or remove the filter in `_shutdown`.

## Green gate for the Coder (from the governing docs)
- Zero failures on the 4-file command above **except** the 4 TestCosmetics order artifacts (which disappear when the files run in full-suite order; the full fast suite must be ZERO failed).
- ES01B config key removal MUST land in the SAME commit as the data_manager/live_trader code (C1 — fleet manifest has ES01B enabled; key-only removal crash-loops on restart).
- C6 BLOCKING before commit: HS14B ledger parity gate (`setup --disable-trailing`, 2200/336) → PARITY: PASS.
- Both new-file Strict-Lock pins that pass at Red must STAY green (trailing presence pin, T7 opt-out pin).

---

## PHASE: Green
**TDD-Coder** | 2026-07-05 06:49 PT | Base: HEAD `f165b9d` (branch `development`, no worktree — worked in place, no running process disturbed)

### Implementation (per blueprint / audit §5, zero test files touched)
1. `src/live_execution/data_manager.py`:
   - `_SHALLOW_BOOTSTRAP_DURATION = "5 D"` module constant (audit §1.2 justification in comment).
   - `__init__` gains keyword-only `allow_shallow_bootstrap: bool = False` (stored as the public
     `self.allow_shallow_bootstrap` the tests pin) + `self.shallow_bootstrapped: bool = False`.
   - `initialize()` Step-1 else-branch: `if self.allow_shallow_bootstrap and self.data_client is
     not None:` → `self._df = self._shallow_bootstrap_from_ibkr(); self.shallow_bootstrapped =
     True; self.save_cache()`; inner else keeps the log.error + FileNotFoundError BYTE-IDENTICAL
     (C2 — fires for allow=True/data_client=None and for every default construction).
   - `_shallow_bootstrap_from_ibkr()`: loud `log.warning` containing "SHALLOW 5M BOOTSTRAP", ONE
     `fetch_historical_bars_by_duration(duration_str="5 D", continuous=True, bar_size=self.bar_size,
     what_to_show="TRADES", use_rth=False)`, `_drop_incomplete_bar`, RuntimeError on empty/None
     BEFORE any cache write (C3), second loud warning with the bar range, returns the frame.
   - `_update_training_ledger()` first-run (ledger-absent) branch gated:
     `if self.allow_shallow_bootstrap and not self.seed_path.exists():` → one loud
     "master-ledger accrual SKIPPED" warning + return (C5 run-2 clean boot; CL seed-present accrual
     and the 5m-model raise untouched; ledger-present append path untouched).
2. `src/live_execution/live_trader.py`:
   - 5m DataManager construction gains `allow_shallow_bootstrap=(self._bar_size in ("1h","2h","4h"))`
     (1h manager UNWIRED — its hard seed requirement stands).
   - `self._shallow_5m_bootstrap: bool = False` in the `__init__` state block.
   - `_warm_start()` latches `self._shallow_5m_bootstrap = bool(getattr(self.data_manager_5m,
     "shallow_bootstrapped", False))` and emits the loud "SHALLOW 5M MODE" banner when True.
   - `start()` Telegram: `Mode: \`5M SHALLOW BOOTSTRAP (no 5m seed — IBKR-fetched window)\``
     stamp appended right after the T7 HOURLY-ONLY stamp block (same pattern).
3. `configs/strategies/ES01B_Sharpe_E03_07042026.json`: removed the `"enable_5m_stream": false`
   line (trailing comma fixed — valid JSON; C1 same-commit sequencing is the MANAGER's commit duty).
4. Docs (audit §5.4, all 7 locations, guidance-only): build-symbol-pipeline.md Phase-1 item 7 +
   Phase-6 Gate 2, run-live.md preflight item 4, add-remove-fleet-model.md ADD prereq + gate (d)
   sentinel-pins command (336d29f lesson), grab-data.md IMPORTANT block, docs/headless-deployment.md,
   deploy/systemd/README.md — new symbols need NO 5m seed (shallow bootstrap is automatic and loud);
   `enable_5m_stream: false` is an explicit opt-out only; 5m models still hard-require a seed.

### Verification (all commands from the ticket)
1. 4-file: `pytest tests/test_shallow_5m_bootstrap.py tests/test_hourly_only_equity_session.py
   tests/test_instrument_context.py tests/test_config_generator_symbols.py -v --tb=short`
   → **4 failed, 146 passed** — the 4 are EXACTLY the pre-documented pre-existing order-dependent
   TestCosmetics artifacts (allowed by the Red handoff's green gate; re-proven order-dependent:
   `pytest tests/test_config_generator_symbols.py tests/test_shallow_5m_bootstrap.py` → 70 passed).
   All 19 new tests green incl. the two Red-passing pins; renamed key-absent pin green.
2. Fence: `pytest tests/test_data_manager.py tests/test_rollover.py
   tests/test_session_watchdog_rollover.py -q` → **108 passed**.
3. Full: `pytest tests/ -q --tb=short -m "not slow"` → **1400 passed, 0 failed** (= 1381 baseline
   incl. the formerly-failing renamed pin + 19 new).

### Notes for the Manager
- `src/live_execution/utils/telegram_alert.py` carries a PRE-EXISTING uncommitted diff (prefix
  param) from another session — NOT part of this ticket; exclude it from this ticket's commit.
- C6 (HS14B ledger parity gate → PARITY: PASS) remains the Manager's pre-commit step.
- Logger-filter leak (TestCosmetics) untouched per scope guard — separate micro-ticket.
