# Impact Review — t6-config-generator-fix_07052026_0043

**Reviewer:** Ticket-Impact-Reviewer | **Date:** 2026-07-05 | **HEAD verified:** 2a8311f (development)
**Mode:** READ-ONLY vs source. Every risk claim below re-verified independently on this machine
(scratchpad script + on-disk checks); no source files touched.

## VERDICT: **APPROVE** (conditional — C1–C4 below; no human authorization required beyond the
D1/D2 rulings the Ticket-Manager has already issued)

---

## 1. Independent verification results (7 risk claims)

### 1.1 Tag helper byte-equivalence — VERIFIED
- Transcribed `gcp/vm_e2e_pipeline.py:652-661` (block A) and `:733-740` (block B) myself and ran a
  268-case differential against the audit §5a helper text: **0 mismatches**. Cases = every real
  basename in `data/processed/*.parquet` (32 files × 8 registry symbols) + targeted edges
  (`bk_` precedence over symbol prefix, case-insensitive match with case-preserving slice,
  `MES_`-style non-prefix, empty-remainder `ES_`).
- Both blocks are true duplicates (same regex, same elif/else, same slice) and both live inside
  `run_pipeline` consuming the same `symbol` parameter (`vm_e2e_pipeline.py:423`), so the helper
  receives identical inputs at both call sites → replacement **cannot** change any current vm_e2e
  output.
- Divergence set helper-vs-current-generator is exactly the modern `{sym}_`-prefixed inputs
  (33/268 cases, e.g. `CL_HourSet_14B`→`HourSet_14B`, `ES_HourSet_01B`→`HourSet_01B`); every
  legacy `bk_` input is identical by construction (bk_ rule fires first in both). This is
  precisely the D1 scope the audit claimed.

### 1.2 CL v2 regression (D1 justification) — VERIFIED
- `reports/batch_runs/batch_20260702_0038_SCOUT_14B_V2/configs/HS14B_Sharpe_E01_07022026.json:16,26-33`
  = `execution_symbol: "CL"` + `E2E_CL_HourSet_14B_*` model_paths → those registry dirs **do not
  exist**; `reports/sweep_hs14b_2x1_6h_scout_20260702-0038/registry/production_output/registry/`
  holds exactly `E2E_HourSet_14B_{long,short}_{logloss,average_precision}`.
- Same for the June-30 batch — local dir is **`batch_20260630_2232_SCOUT_14B_FAIL`** (audit cites
  the bare `batch_20260630_2232`; substance correct, name imprecise — see C3): configs carry
  `E2E_CL_HourSet_14B_*`; `sweep_hs14b_2x1_3h_scout_20260630-2232/.../registry/` holds
  `E2E_HourSet_14B_*` only.
- ZC batches (`batch_20260704_0334`, `_2215`) show the identical pattern (`E2E_ZC_HourSet_01A_*`
  emitted; on-disk dirs are `E2E_HourSet_01A_*`).
- Live prod `configs/strategies/HS14B_Sharpe_E01_06262026.json` verified untouched-and-valid:
  its pre-stripping-era `canary_output/.../E2E_CL_HourSet_14B_*` paths exist on disk (both
  final_model.pkl present).
- **Conclusion: byte-pinning current v2-CL generator output would pin dead paths. D1 (manager-
  accepted) is the correct call.**

### 1.3 ES01B surgical-patch round-trip — RE-EXECUTED, VERIFIED
In-memory (no file writes): applied the §2 table to the shipped config and called
`resolve_instrument_context`:
- Resolves `execution=ES, brain=ES, exchange=CME` (multiplier 50, tick 0.25).
- All 4 referenced files exist: both `E2E_HourSet_01B_{long,short}_logloss/final_model.pkl`
  (2,339,861 / 1,279,766 B) and
  `reports/batch_runs/batch_20260704_0701_ES_01B_SCOUT/predictions/ES01B_Sharpe_E03_predictions.csv`
  (1,531,843 B; the old `batch_20260704_0701/...` path confirmed absent — the rename finding is real).
- Negative control: the **unpatched** shipped config raises ValueError (model-symbol mismatch) —
  the current T1 pin's behavior, reproduced.
- Tamper control: patched config with `models.long.symbol` flipped to "CL" raises → the generator's
  proposed post-emission self-check has real teeth.
- `derive_model_symbol("E2E_HourSet_01B_long_logloss")` returns None → confirms D2's rationale:
  post-T6 stripped ids make the opportunistic check permanently dead; the explicit field is the
  only surviving cross-check. Unconditional emission (manager-accepted) is architecturally right.
- Shipped config is SHA256-identical to the batch copy (audit's "byte-identical" claim checked).

### 1.4 Two-and-only-two T1 pin flips — VERIFIED
Repo-wide grep (excluding data/, reports/) for ES01B / intended-failure semantics: the ONLY tests
depending on the shipped file failing are
`tests/test_instrument_context.py::TestShippedConfigs::test_es01b_shipped_config_raises_intended_failure`
(:273, docstring: "REFUSE to resolve until T6") and
`::test_all_shipped_configs_resolve_except_es01b` (:305, `intended_failures` set at :312).
`:186` and the TestModelSymbolCrossCheck tests use synthetic dicts (unaffected). No other test
file globs `configs/strategies` (checked `_CONFIG_DIR`/glob census across tests/). Nothing in
`scripts/` or `.agents/workflows/` references ES01B. **Exactly two pins flip; the Strict-Lock
evolution is as narrow as claimed.**

### 1.5 batch_post_optimizer emits no strategy configs — VERIFIED, with one census gap (C1)
- Full write census of `agent/batch_post_optimizer.py`: report .md (:791), optimization-results
  JSON (:815-816), merged prediction CSVs (:1122, :1186). **No strategy-config emission** — the
  blueprint's `:1045-1074` is base-config *loading*: `base_cfg` (:1045-1052) feeds only the
  baseline-parameter display columns in `generate_optimized_report` (:718-731); `base_config_path`
  (:1071-1075) is handed to the optimizer as the optimization base (:1134). "No T6 change" stands.
- **However, the audit's §1f census ("full census") missed one emission site and over-claims
  row #3:** `agent/strategy_optimizer.py::run_optimization` writes
  `{config_path stem}_opt_{suffix}.json` **next to the input config** (:1443-1447; hybrid twin
  :1868-1872). When invoked by batch_post_optimizer's target-pairs path the input config is
  `configs/strategies/<base>.json`, so these land **in the shipped configs dir** — on-disk proof:
  `configs/strategies/hourly_ensemble_010_opt_sharpe.json` and `_opt_sortino.json` exist today,
  and they ARE inside T1's fleet-wide resolve glob. Additionally, census row #3's "TopKTracker
  candidates fixed transitively by #2" holds only for vm_e2e-invoked runs; batch_post_optimizer's
  target-pairs path passes the raw CL base at :1134, so candidates AND `_opt_*.json` from a
  future non-CL batch would still carry `execution_symbol: "CL"` (with no models symbol tags →
  they resolve silently as CL). This does not change the T6 diff — the write lives in
  strategy_optimizer, and these are non-promoted side artifacts — but it is exactly the bug class
  this program is closing and must be documented/routed (condition C1).

### 1.6 CL byte-identity of generator output — VERIFIED (analytical + differential)
- `bk_` inputs: derivation unchanged by construction (differential: 0 diffs on every bk_ case).
- `cfg["execution_symbol"] = baseline_symbol`: base `hourly_ensemble_010.json` carries the key at
  :16 with value "CL" → in-place overwrite, same value, insertion order preserved (deep copy is a
  json round-trip; json.dump(indent=4) is deterministic) → zero byte change for CL.
- `models.*.symbol`: genuinely new key per side, appended last in each side's dict → exactly one
  added line per side, nothing else moves. The audit's "deep-equal modulo exactly
  models.{long,short}.symbol" pin is achievable as specified. No float/whitespace drift vectors:
  no other field is rewritten with different values, `json.dump` settings unchanged.
- `baseline.symbol` presence re-verified in real manifests: CL 14B v2 scout = "CL",
  ES 01B = "ES", ZC = "ZC", 20260630 CL v2 = "CL". Fail-fast RAISE is safe for all v2 dirs;
  legacy dirs crashing on regeneration is house-rule-compliant as enumerated.
- Noted (C2): the ES v2 manifest has **no `defaults` block at all**, so the generator's
  `strategy_config` fallback (`generate_ensemble_artifacts.py:272`, default
  `hourly_ensemble_010.json`) is a pre-existing **silent default** the proposal does not remove.
  Post-T6 it is symbol-benign (execution_symbol + models.*.symbol get stamped from
  baseline.symbol), but it grazes the no-silent-defaults house rule and must be recorded.

### 1.7 Wrapper KEEP — VERIFIED coherent
`scripts/download_ibkr_history.py:45` imports and `:95` calls `build_cl_contract(continuous=True)`
— a real production script caller. The wrappers (`ibkr_client.py:71,99`) are thin delegations to
the registry-driven `build_future_contract` (:30, raises on unknown symbols — no silent CL
fallback in the generic path). Calling an explicitly-named `build_cl_contract` is an explicit CL
request, not a silent default → KEEP does not conflict with the house rule. Deletion would churn
two Strict-Lock files (`test_build_future_contract.py:84-298`, `test_symbol_data_paths.py:262-291`)
plus a script migration for zero behavioral gain. KEEP approved.

---

## 2. Constraint evaluation (workflow rules)

| Rule | Finding |
|---|---|
| **Interface Rule** | NOT triggered. No existing signature changes; `derive_dataset_tag` is a new stdlib leaf; generator/vm_e2e edits are internal to their scripts; resolver untouched. |
| **Base Class Rule** | NOT triggered. New leaf added to `src/core` (additive); `instrument_context.py` and `instrument_master.py` unmodified. |
| **Refactor Veto** | NOT triggered. Multi-file but zero component rewrites — every edit is a surgical, pinned, value-preserving-for-CL change (helper extraction + call-site swap, field stamping, one config patch, two self-documented pin flips, cosmetic strings). The two genuinely behavioral candidates (m2 rename, warmup entry_crossed) are correctly deferred to spin-offs. |
| **Byte-identity deviations D1/D2** | Require ack per the ticket's own constraint — **already ruled accepted by the Ticket-Manager**; my §1.2/§1.3 verification confirms both rulings are evidence-backed (D1 pins a proven defect otherwise; D2 is the only surviving cross-check). |

## 3. Conditions of approval

- **C1 (census correction + routing, MEDIUM):** Amend audit §1f before/with TDD: add emission site
  #8 = `agent/strategy_optimizer.py:1443-1447` (+ hybrid `:1868-1872`) writing
  `{stem}_opt_{suffix}.json` into the input config's directory (= `configs/strategies/` for
  batch_post_optimizer target-pairs runs; on-disk proof `hourly_ensemble_010_opt_{sharpe,sortino}.json`),
  and correct row #3: TopKTracker candidates are fixed by the 5c one-liner **only** for
  vm_e2e-invoked runs — batch_post_optimizer's target-pairs path (:1134) passes the raw CL base,
  so non-CL batches will still emit CL-labeled candidates/`_opt` configs. Route this residual to a
  spin-off (T8 promotion gate or its own micro-ticket). NO T6 code change required — do not widen
  the diff.
- **C2 (status flag):** Record in ticket_status the pre-existing silent default at
  `generate_ensemble_artifacts.py:272` (`strategy_config` → `hourly_ensemble_010.json`; ES v2
  manifest verified to carry NO defaults block) as accepted/deferred — symbol-benign post-T6 but
  a no-silent-defaults house-rule tension for a future ticket.
- **C3 (doc precision):** Cite the June-30 batch by its actual local name
  `batch_20260630_2232_SCOUT_14B_FAIL` in final artifacts.
- **C4 (gate, from audit §6 — made a hard condition):** Full fast suite green **plus** the HS14B
  ledger parity gate re-run showing PARITY: PASS / $0.00 delta (the live_trader/ibkr_client
  display+string edits ride this fence), before merge.

## 4. Blast-radius map (files the Coder may touch)
- NEW `src/core/dataset_tag.py` (leaf, stdlib-only).
- `agent/generate_ensemble_artifacts.py` (tag call-site, baseline.symbol fail-fast, field stamping,
  self-check).
- `gcp/vm_e2e_pipeline.py` (two tag blocks → helper; `ensemble_cfg["execution_symbol"]` one-liner —
  manager-approved include).
- `configs/strategies/ES01B_Sharpe_E03_07042026.json` (§2 table only).
- `tests/test_instrument_context.py` (the two pins only), NEW `tests/test_generator_symbol_emission.py`.
- Cosmetics per §5f: `src/live_execution/data_manager.py` (backup filenames),
  `tests/smoke_test_pipeline.py` (cadence regex), `live_trader.py:3138-3139` (m1 display) +
  narrow m3 strings (`live_trader.py:3356,1920,1928-1930`, `ibkr_client.py:424,432`, `cli.py:158`).
- NOT to be touched: batch_post_optimizer, strategy_optimizer, fleet_runner, resolver/registry,
  wrapper functions, `CLOnlyLogFilter`, warmup entry_crossed block, `cli.py:118` title.

*Reviewer verification artifacts: scratchpad script `t6_verify.py` (268-case helper differential,
ES01B round-trip + negative/tamper controls, path existence) — session scratchpad, not committed.*
