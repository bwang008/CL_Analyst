# TDD Result — t6-config-generator-fix_07052026_0043

**Outcome: GREEN + PARITY PASS — ticket complete. Reviewer verdict: APPROVE (C1-C4).**

- Red: 51 new tests (43 failing + 15 CL pins passing — oracle-verified byte-faithful)
  + the two sanctioned T1 Strict-Lock pin flips; manager-verified baseline:
  exactly 2 failed / 1282 passed.
- Green: **1335 passed, 0 failed** (manager-verified independently).
- C4 blocking parity gate: **PARITY: PASS**, exit 0 — $0.00 delta ($1,695.01 both).

## Files changed
- NEW `src/core/dataset_tag.py` — shared derive_dataset_tag (byte-for-byte vm_e2e
  logic; 268-case differential + identity pins ensure generator and VM pipeline can
  never diverge again).
- `agent/generate_ensemble_artifacts.py` — baseline.symbol REQUIRED (raise);
  execution_symbol + models.*.symbol stamped; post-emission resolve_instrument_context
  self-check raises; inline tag regex removed.
- `gcp/vm_e2e_pipeline.py` — both tag blocks → shared helper; ensemble_cfg emission
  stamps symbols.
- `configs/strategies/ES01B_Sharpe_E03_07042026.json` — surgical 10-field patch:
  NOW RESOLVES as ES/ES/CME; model_paths → real E2E_HourSet_01B_* dirs;
  predictions → batch_20260704_0701_ES_01B_SCOUT (all artifacts verified on disk).
- Cosmetics (§5f, CL byte-identical pins): per-symbol backup filenames, smoke cadence
  regex, [PNL] display via instrument multiplier, symbol-derived log/banner/error
  strings.
- `tests/test_config_generator_symbols.py` — NEW, 51 tests (Strict-Lock).
- `tests/test_instrument_context.py` — the ONE sanctioned evolution: both
  "until T6" pins flipped (ES01B resolves; fleet-wide zero intended failures).

## Key finding preserved
The tag regression bit CL too: v2 CL batches (batch_20260630_2232_SCOUT_14B_FAIL,
batch_20260702_0038_SCOUT_14B_V2) emitted configs with model_paths at NONEXISTENT
E2E_CL_* dirs. Live prod HS14B config safe only because it predates the stripping.

## Routed onward
- T8: C1 residual (strategy_optimizer.py:1443-1447/:1868-1872 writes _opt_/_hybrid_
  configs into configs/strategies from the raw CL base — future non-CL batches would
  emit CL-labeled candidates via the target-pairs path) + C2 (generator :272 silent
  CL-base default when manifest lacks `defaults`).
- Spin-off micro-tickets (not minted yet): m2 cl_* account-summary key rename;
  warmup entry_crossed fix.
- T7 preconditions standing: ES equity session shape (15:15-15:30 CT halt),
  GVZ entitlement check, ES 1h seed parquet (ES_raw_1h.parquet not yet built).
