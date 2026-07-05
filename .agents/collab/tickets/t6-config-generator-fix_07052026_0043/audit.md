# Audit — t6-config-generator-fix_07052026_0043

**Auditor:** Ticket-Auditor | **Date:** 2026-07-05 | **HEAD:** 2a8311f (development; T1-T5 + modify_order fix merged)
**Scope source:** gap blueprint `multi-symbol-live-gaps_07042026_1520/blueprint.md` (T6 section, "Config Generator Fix", Gap rows B7/m1/m2/m3) + routed deferrals from T2/T3/T5 tdd_results.
**Mode:** read-only vs source; all verification commands re-run at HEAD on this machine.

---

## 1. Bug chain — verified at HEAD, exact lines

### 1a. Generator prefix derivation (`agent/generate_ensemble_artifacts.py:323-327`)
```python
# Derive e2e_dataset_tag the exact same way vm_e2e_pipeline.py does   <-- comment is FALSE
data_basename = os.path.splitext(os.path.basename(args.data))[0]
match = re.search(r'bk_(.+)$', data_basename)
e2e_dataset_tag = match.group(1) if match else data_basename
e2e_prefix = f"E2E_{e2e_dataset_tag}"
```
Only the legacy `bk_` rule. `gcp/vm_e2e_pipeline.py:653-663` additionally strips the modern
`{symbol}_` prefix (`elif data_basename.upper().startswith(symbol.upper() + "_"): dataset_tag =
data_basename[len(symbol)+1:]`) before building `bundle_name = f"E2E_{dataset_tag}_{direction}_{metric_name}"`.
The identical stripping block is DUPLICATED at `vm_e2e_pipeline.py:733-740` (ensemble-config tag).
Result for `--data data/processed/ES_HourSet_01B.parquet` (the exact vm_post_optimize.sh:429
invocation): generator emits `E2E_ES_HourSet_01B_*` while the registry bundles on disk are
`E2E_HourSet_01B_*`.

**Git provenance (bounded per workflow rule):** the `{symbol}_` stripping entered vm_e2e_pipeline at
a239197/7ce89b8 (2026-06-29, "Multi-symbol support"); the generator's derivation was added
7920eba/6bf3209 (2026-06-30/07-01) with only the `bk_` rule and a comment *claiming* alignment.
→ the prefix half of the bug is a **regression-by-divergence** (two derivations that were
specified to be identical drifted apart at the multi-symbol change).

### 1b. execution_symbol leak (`agent/generate_ensemble_artifacts.py:272-278, 354-357`)
Base config = `manifest.defaults.strategy_config` defaulting to `hourly_ensemble_010.json`
(a CL config, `execution_symbol: "CL"`, key order verified). Deep copy at :354
(`json.loads(json.dumps(base_config))`), then only `nickname/description/holdout_months/
conflict_resolution/models` are overridden (:355-437). `execution_symbol` is **never touched**;
`manifest.baseline.symbol` (present in every v2 manifest, verified: CL 14A/14B/15A scout+canary
= "CL", ES 01B = "ES", ZC 01B = "ZC") is **never read**. Day-one latent gap, **not** a regression
— exposed by the first non-CL manifest.

### 1c. Written proof of both defects in shipped/batch artifacts
- `configs/strategies/ES01B_Sharpe_E03_07042026.json:16` `"execution_symbol": "CL"`; `:26-34`
  `E2E_ES_HourSet_01B_{long,short}_logloss` experiment_ids/model_paths. Byte-identical to the batch
  copy `reports/batch_runs/batch_20260704_0701_ES_01B_SCOUT/configs/ES01B_Sharpe_E03_07042026.json`
  (diff verified).
- On-disk registry (verified): `reports/sweep_es01b_2x1_6h_scout_20260704-0701/registry/
  production_output/registry/` contains exactly `E2E_HourSet_01B_{long,short}_{logloss,
  average_precision}`; `final_model.pkl` present (long_logloss 2,339,861 B; short_logloss
  1,279,766 B). **Models were local all along; the config paths are wrong.**
- Same bug in every non-CL batch: `reports/batch_runs/batch_20260704_0334/configs/ZC_Sharpe_E01_*.json`
  and `batch_20260704_2215/configs/*` all carry `execution_symbol: CL` + `E2E_ZC_HourSet_01A_*`
  paths (dirs don't exist — real dirs are `E2E_HourSet_01A_*`). NG/GC batches will match. None of
  these are shipped to `configs/strategies/` — only ES01B E03 was promoted.

### 1d. **NEW FINDING — CL v2 batches are ALSO broken** (materially changes the byte-identity constraint)
For v2 CL inputs the data basename is `CL_HourSet_14B` (no `bk_`), so the generator emits
`E2E_CL_HourSet_14B_*` while vm_e2e's stripped registry dirs are `E2E_HourSet_14B_*`. Verified:
- `reports/batch_runs/batch_20260702_0038_SCOUT_14B_V2/configs/HS14B_Sharpe_E01_07022026.json`
  model_path `.../production_output/registry/E2E_CL_HourSet_14B_long_logloss/final_model.pkl` —
  that dir does NOT exist; `reports/sweep_hs14b_2x1_6h_scout_20260702-0038/registry/
  production_output/registry/` holds `E2E_HourSet_14B_*`. Same for batch_20260630_2232.
- The live prod config `configs/strategies/HS14B_Sharpe_E01_06262026.json` is fine only because it
  comes from the PRE-stripping era (2026-06-26 canary_output layout, whose on-disk dirs really are
  `E2E_CL_HourSet_14B_*` — verified). Legacy-era `bk_` inputs (HS13A etc.) derive correctly.
→ "today's generator output for CL inputs" is **correct for legacy `bk_` inputs and WRONG for v2
`CL_*` inputs**. See §4 constraint analysis.

### 1e. `agent/batch_post_optimizer.py:1045-1074` — blueprint correction
Audited in full: batch_post_optimizer **does not emit strategy configs**. `base_cfg` (:1045-1052)
is used only for baseline-parameter *display* in reports (:718-731); `base_config_path`
(:1071-1075) is handed to the optimizer as the ensemble optimization base (:1134); its
`json.dump` (:816) writes optimization-results JSON, not configs. The CL-base *pattern* it shares
is real but the emission actually happens in (i) the generator and (ii) vm_e2e_pipeline (below).
**No batch_post_optimizer code change is required for T6.**

### 1f. Full census of strategy-config emission paths (grep: `json.dump(cfg|config...)`, deep copies, execution_symbol writes)
| # | Site | What it writes | execution_symbol handling | T6 action |
|---|------|----------------|---------------------------|-----------|
| 1 | `agent/generate_ensemble_artifacts.py:354,440-442` | `{batch_dir}/configs/{TAG}_{Obj}_E{NN}_{date}.json` — the live-promoted configs | leaked from CL base | **FIX (primary)** |
| 2 | `gcp/vm_e2e_pipeline.py:730,742-754,781-795,850,865` | `ensemble_cfg` → `{fmt_prefix}_{metric}.json` / `sweep_{metric}.json` in production_output (optimization bases; feed batch_post_optimizer :1171 and TopKTracker candidates) | `dict(strategy_cfg)` copy of CL base; never set | **FIX (1-line propagation, recommended)** + de-dup tag blocks :653-661, :733-740 |
| 3 | `agent/strategy_optimizer.py:280-290` `TopKTracker.save_best` | `configs/strategies/candidates/Rank_*.json` | inherits from #2's ensemble_cfg | fixed transitively by #2; no change |
| 4 | `agent/sweep_ensembles.py:238-277` | temp backtest config (legacy subprocess mode) | inherits base; never shipped live | no change (legacy, non-live) |
| 5 | `agent/execution_param_sweeper.py:30-65` | `optuna_temp.json` | inherits base; temp | no change |
| 6 | `gcp/vm_e2e_pipeline.py:365-379` | registry-bundle `experiment_config.json`/`config.json` | n/a (metadata, no execution_symbol) | no change |
| 7 | `scripts/ledger_parity_check.py:148`, `scripts/update_json.py`, `scripts/update_targets.py` | harness/one-off utilities | n/a | no change |

---

## 2. Real on-disk ES artifact layout + exact corrected ES01B contents

**predictions_path finding (corrects gap-table m4):** the batch predictions ARE synced locally —
the gap analysis looked for `reports/batch_runs/batch_20260704_0701/` (the VM-side dir name baked
into the config by `pred_path_workspace = os.path.join(args.batch_dir, ...)` at :382), but the
local sync was renamed `batch_20260704_0701_ES_01B_SCOUT/`. Verified present:
`reports/batch_runs/batch_20260704_0701_ES_01B_SCOUT/predictions/ES01B_Sharpe_E03_predictions.csv`
(1,531,843 B, header `DateTime,prob_Buy,prob_Sell`). **The config can be made fully valid locally;
no T7 sync step is a prerequisite.** (T7 still owns ES seed/macro/COT data + IBKR entitlements.)

**Surgical patch — the complete field delta for `configs/strategies/ES01B_Sharpe_E03_07042026.json`:**
| Field | From | To |
|---|---|---|
| `execution_symbol` | `"CL"` | `"ES"` |
| `models.long.experiment_id` | `E2E_ES_HourSet_01B_long_logloss` | `E2E_HourSet_01B_long_logloss` |
| `models.long.model_path` | `reports/sweep_es01b_2x1_6h_scout_20260704-0701/registry/production_output/registry/E2E_ES_HourSet_01B_long_logloss/final_model.pkl` | same path with `E2E_HourSet_01B_long_logloss` |
| `models.long.predictions_path` | `reports/batch_runs/batch_20260704_0701/predictions/ES01B_Sharpe_E03_predictions.csv` | `reports/batch_runs/batch_20260704_0701_ES_01B_SCOUT/predictions/ES01B_Sharpe_E03_predictions.csv` |
| `models.long.symbol` | (absent) | `"ES"` (NEW — T1 handshake) |
| `models.short.*` | mirror of long with `short_logloss` | mirror |
| everything else | unchanged | unchanged (params/tiers/live_config/client_id 1010/holdout/conflict_resolution) |

**Round-trip PROVEN at HEAD** (executed in-memory): patched config →
`resolve_instrument_context` → `execution_symbol=ES, brain=ES, exchange=CME`; both
`model_path` files and both `predictions_path` files exist locally. Do NOT add `brain_symbol`
(ES is its own brain; structural derivation covers it).

The 7 sibling batch configs (E01/E02/E04, Sortino E01-E04) stay broken in the batch dir — they are
unshipped generated artifacts; regenerating/promoting them is T7/T8 territory (T8's promotion gate
is the process fix).

---

## 3. Patch vs regenerate — decision: **SURGICAL PATCH** (with test-enforced validation)

Local regeneration was assessed concretely: inputs exist (`manifest.json` w/ baseline.symbol=ES,
`optimization_results_ensembles_*.json`, `top_pairs.json`, `data/processed/ES_HourSet_01B.parquet`,
per-side prediction CSVs in the sweep tree). It is *technically* runnable offline, but rejected:
1. `main()` unconditionally runs 8 backtest subprocesses and rewrites the batch dir's .md reports
   and predictions/ — heavy side effects on a historical artifact for zero informational gain.
2. The merged `_merged_ens_*.csv` files were never synced (verified absent) → `use_merged=False`
   → regenerated `predictions_path` would point at per-side sweep CSVs, DIVERGING from the shipped,
   already-validated merged predictions file. The surgical patch keeps the exact artifact the batch
   validated.
3. Only 1 of 8 configs is shipped; regeneration churns all 8 plus reports.
4. Validation is stronger as tests: the flipped T1 pins + a new shipped-config test assert the
   exact patched values, resolver pass, and on-disk existence — deterministic and reviewable,
   equivalent to a "validation script" but permanent.
The generator fix itself is proven separately by offline fixture round-trips (§6), which is
strictly better evidence than one ES regeneration.

---

## 4. Hard-constraint analysis — two byte-identity deviations REQUIRING ACK

Constraint as ticketed: "CL configs and CL generator outputs byte-identical (emit exactly today's
output for CL inputs — regression pin via fixture)."

- **Shipped CL configs:** untouched — fully honored. `hourly_ensemble_010.json` base has
  `execution_symbol` as an existing key, so `cfg["execution_symbol"] = "CL"` overwrites in place
  (insertion order preserved → no byte change from this line for CL).
- **D1 (unavoidable):** for **v2 CL inputs** (`CL_HourSet_14B` style) today's output is
  `E2E_CL_HourSet_14B_*` pointing at directories that DO NOT EXIST (§1d, verified). Literal
  byte-identity would pin a proven defect. Proposal: fixture pins **legacy `bk_` CL input
  byte-identical** (derivation path unchanged by construction) and pins **v2 CL input to the
  corrected `E2E_HourSet_14B_*`** with fixture-tree existence. This is the blueprint's own B7 fix
  applied to CL, but it deviates from the constraint's letter → **needs manager/human ack**.
- **D2 (design choice):** emitting `models.<side>.symbol` (ticket-endorsed handshake) adds a new
  key to CL outputs too. Recommendation: **unconditional emission**. Rationale: post-T6
  experiment_ids are symbol-stripped (`E2E_HourSet_01B_*`), so `derive_model_symbol` returns None
  and the opportunistic check is permanently dead — the explicit field is the ONLY surviving
  model↔symbol cross-check; making it CL-conditional would re-introduce a symbol-conditional
  silent gap of exactly the class this program is eliminating. CL fixture then pins: output
  deep-equal to today's golden **modulo exactly** `models.long.symbol`/`models.short.symbol`
  == "CL" (and those keys hard-validate under T1). Literal-compliance alternative (emit only for
  non-CL) is drafted in the blueprint as option D2-alt if the reviewer insists → **needs ack**.
- No-silent-defaults: generator RAISES (`ValueError`, FATAL wording) when `manifest.baseline.symbol`
  missing/empty, and validates it via `get_instrument()` (raises on unknown). All v2 manifests carry
  it (verified); legacy v1 batch dirs without it will now crash on regeneration — house-rule
  compliant, enumerated as accepted behavior.
- Scope guards honored: no fleet_runner, no training-logic change (vm_e2e edits are pure
  tag-de-dup + a value-preserving symbol propagation), live-engine edits confined to
  display/log-string cosmetics with CL-output-identical proof (§5c) — interpreted per the ticket's
  own m1/m2/m3 routing as "no live engine *behavior* changes".

---

## 5. Localized design

### 5a. Shared tag helper (new, leaf)
`src/core/dataset_tag.py` — stdlib-only:
```python
def derive_dataset_tag(data_basename: str, symbol: str) -> str:
    m = re.search(r"bk_(.+)$", data_basename)
    if m:
        return m.group(1)
    if data_basename.upper().startswith(symbol.upper() + "_"):
        return data_basename[len(symbol) + 1:]
    return data_basename
```
Byte-for-byte the vm_e2e_pipeline:655-661 logic (case-insensitive match, case-preserving slice).
Placed in `src/core` because both consumers already import from `src` (vm_e2e_pipeline.py:58
`src.util`, :1135 `src.core.instrument_master`; generator lazily imports `src.data_paths`).

### 5b. Generator (`agent/generate_ensemble_artifacts.py`)
1. After manifest load (~:251): `baseline_symbol = manifest.get("baseline", {}).get("symbol")`;
   RAISE ValueError if falsy; `get_instrument(baseline_symbol)` to fail-fast unknown symbols.
2. Replace :324-327 with `e2e_dataset_tag = derive_dataset_tag(data_basename, baseline_symbol)`;
   delete the stale "exact same way" comment (now true by construction).
3. After the deep copy (:354-357): `cfg["execution_symbol"] = baseline_symbol`.
4. In the models block (:395-401): `cfg["models"]["long"]["symbol"] = baseline_symbol` (short
   mirror) — subject to D2 ack.
5. **Post-emission self-check (round-trip, before `json.dump`):**
   `from src.live_execution.instrument_context import resolve_instrument_context` (leaf-safe,
   VM-safe) → `resolve_instrument_context(cfg)`; failure RAISES (a config that cannot start is a
   batch failure, not a warning). Plus `os.path.isfile(model_path)` → loud WARN only (cloud-side
   generation may reference not-yet-synced trees; per blueprint "warn not raise").

### 5c. vm_e2e_pipeline (`gcp/`) — tag/emission alignment only
- Replace the two duplicated stripping blocks (:653-661 and :733-740) with `derive_dataset_tag`
  calls (logic-identical; removes the divergence-by-copy hazard permanently).
- Recommended 1-liner at :730 area: `ensemble_cfg["execution_symbol"] = symbol` — kills the same
  leak in emission sites #2/#3 (sweep_{metric}.json + TopKTracker candidates). Value-preserving
  for CL (symbol=="CL"). Severable if the impact reviewer wants the minimum diff.

### 5d. ES01B surgical patch — §2 table; the ONLY shipped-config content change in T6.

### 5e. T1 Strict-Lock evolution — exactly TWO pins flip (both self-documented as T6-pending)
`tests/test_instrument_context.py`:
1. `TestShippedConfigs::test_es01b_shipped_config_raises_intended_failure` (:273-282) — docstring
   says "It must REFUSE to resolve **until T6 regenerates it**"; impact_review C4 called it the
   intended-failure documentation. Flips to `test_es01b_shipped_config_resolves_as_es`: asserts
   execution_symbol/brain == "ES", exchange == "CME", `models.*.symbol == "ES"`, both model_paths
   + predictions_paths exist on disk.
2. `TestShippedConfigs::test_all_shipped_configs_resolve_except_es01b` (:305-323) — the
   `intended_failures = {"ES01B_Sharpe_E03_07042026.json"}` set (docstring: "until T6 regenerates
   it") becomes empty; rename `test_all_shipped_configs_resolve`.
No other test in the repo references ES01B or the intended-failure (grep-verified). This is the
one authorized Strict-Lock evolution; every other T1/T2/T3/T5 pin stays untouched.

### 5f. Deferred/routed items — decisions
| Item | Decision | Evidence |
|---|---|---|
| `build_cl_contract`/`build_mcl_contract` wrapper deletion (T3→T6) | **KEEP** | Production caller remains: `scripts/download_ibkr_history.py:45,95`; wrappers serve as regression oracles inside two Strict-Lock files (`test_build_future_contract.py:84-298`, `test_symbol_data_paths.py:262-291`). Deletion = Strict-Lock churn + script migration for zero behavioral gain; wrappers are explicit-by-name (not silent defaults) and delegate to the registry-driven builder. |
| Per-symbol backup filenames `_backup_cache_to_repo` (T2 5.3 → T6) | **INCLUDE** | `data_manager.py:766-802`: cache backup name → `f"{self.cache_path.stem}_{ts}_{reason}.parquet"` (CL stem is `warm_start_cache` → byte-identical legacy names; ES → `warm_start_cache_ES_...`); roll-metadata backup → `f"{Path(self.roll_metadata_path).stem.lstrip('.')}_{ts}_{reason}.json"` (CL `.roll_metadata` → `roll_metadata` — today's literal preserved; ES → `roll_metadata_ES`). Prevents cross-symbol backup collision/ambiguity in a fleet. |
| smoke_test cadence regex (T2 → T6) | **INCLUDE** | `tests/smoke_test_pipeline.py:250-265`: extend `_expected_cache_timestep` for `warm_start_cache_{SYM}.parquet` → 5m and `warm_start_cache_{SYM}_{N}{mh}.parquet` → N m/h; legacy names untouched. Test-infra only. |
| `live_config.seed_path_5m`/`cache_path` keys (T2 → T6) | **DEFER (close as won't-do)** | `--seed-path/--cache-path` CLI flags + T2's `derive_data_paths` per-symbol defaults cover every case; config keys would be a second source of truth AND emitting them would break CL generator byte-identity. Same reasoning: do NOT emit blueprint-optional `live_config.seed_path_1h` (override plumbing already exists at live_trader.py:394; the derived default `ES_raw_1h.parquet` is correct without it). |
| m1 — `avg_cost / 1000.0` | **INCLUDE display site only**: `live_trader.py:3138-3139` → `/ self._execution_instrument.multiplier` (CL multiplier == 1000, registry-verified → CL byte-identical [PNL] output; ES becomes correct at /50). **EXCLUDE** the warmup `entry_crossed` comparison (`live_trader.py:2103-2136` compares raw averageCost to bar High/Low): fixing it CHANGES CL warmup behavior (entry_crossed currently always-False for CL; gap table itself calls it benign). Pin current behavior; note as follow-up micro-ticket. |
| m2 — `cl_*` account-summary keys, `get_cl_position`/`close_cl_position*` names + `symbol="CL"` defaults | **DEFER to dedicated micro-ticket** | Functionally correct today (values filtered by passed symbol; every production call site passes `symbol=` explicitly — census: live_trader :553,:1895,:2061,:3135,:3788 + adapters). Rename touches 2 producers (`ibkr_client.py:744-791`, `simulated_execution.py:179-181`), ≥9 live_trader consumer lines, and Strict-Lock/regression tests (`test_account_summary.py`, `test_tick_order_pricing.py:558-712`, `test_bracket_order.py:346+` rely on the defaults). Disproportionate blast radius for a cosmetic inside the ticket whose core is the generator; "one coordinated change" (gap table's own words) fits a follow-up micro-ticket. |
| m3 — cosmetic CL strings | **INCLUDE narrow sweep**: `live_trader.py:3356` dry-run `"... %d CL"` → `%s` with `self._execution_symbol`; `ibkr_client.py:424,432` `"Failed to qualify CL contract"` → include actual contract symbol; `live_trader.py:1920,1928-1930` account banner "(CL Only)"/"CL Avg Cost" → symbol-derived text (dict keys unchanged pending m2); `cli.py:158` "Number of CL contracts" → "contracts". **LEAVE**: `CLOnlyLogFilter` (log_config.py:53 — VERIFIED DEAD in production: never `addFilter`ed anywhere in src, only re-exported at live_trader.py:99 and instantiated by tests/test_log_filter.py; renaming churns a pinned test for dead code — document, don't touch); `cli.py:118` program title "CL Analyst" (product name); `close_cl_position*` method names (→ m2 micro-ticket); data_manager docstrings only where a pure comment edit. |

### 5g. Severity + regression classification
- **Severity: MEDIUM/HIGH** (multi-line, multi-file: 1 new leaf module, generator, vm_e2e de-dup,
  1 shipped-config patch, 2 test-pin flips, narrow cosmetic sweep — but zero structural refactor;
  every change is either value-preserving for CL or pinned).
- **Regression: PARTIAL YES.** Prefix mismatch = regression-by-divergence since 2026-06-29/30
  (vm_e2e a239197/7ce89b8 added stripping; generator 7920eba/6bf3209 never followed despite its
  comment). execution_symbol leak = latent day-one gap (not a regression). CL v2 batches
  (20260630_2232, 20260702_0038) already produced dead model_paths — the regression has been
  biting CL, not just ES.

---

## 6. TDD test list (new Strict-Lock file `tests/test_generator_symbol_emission.py` unless noted)

Tag helper:
1. `derive_dataset_tag` table: (`cl-5m_bk_HourSet_13A`,CL)→`HourSet_13A`; (`CL_HourSet_14B`,CL)→`HourSet_14B`; (`ES_HourSet_01B`,ES)→`HourSet_01B`; (`HourSet_09`,CL)→`HourSet_09`; case-insensitive prefix match with case-preserving remainder; symbol not a prefix → passthrough.
2. Alignment pin: vm_e2e_pipeline and the generator both import THE SAME function (assert `agent.generate_ensemble_artifacts.derive_dataset_tag is src.core.dataset_tag.derive_dataset_tag`, same for gcp module) — divergence structurally impossible.

Generator fixture round-trips (tmp batch dir: manifest + opt-results + top_pairs + fake sweep tree; `subprocess.run` monkeypatched to skip backtests):
3. **CL legacy (`bk_`) input**: emitted config byte-identical to golden EXCEPT exactly `models.{long,short}.symbol == "CL"` (D2; if D2-alt is chosen, fully byte-identical). Golden captured from HEAD behavior.
4. **CL v2 input (`CL_HourSet_14B`)**: model_path/experiment_id contain `E2E_HourSet_14B_*` and model_path exists in fixture tree (D1 fix pin); all other bytes golden-identical.
5. **ES manifest**: `execution_symbol == "ES"`, `models.*.symbol == "ES"`, model_path contains `E2E_HourSet_01B_long_logloss` and exists in fixture tree.
6. Missing `baseline.symbol` → ValueError (message pinned, FATAL wording); unknown symbol (`"XX"`) → raises via get_instrument.
7. Self-check: emitted config passes `resolve_instrument_context`; a doctored base config that would yield a mismatch (e.g. tampered `models.long.symbol`) makes the generator RAISE before/at dump; missing model_path on disk → WARN (capsys), not raise.
8. vm_e2e `ensemble_cfg["execution_symbol"]` propagation unit (if 5c one-liner accepted): dict carries manifest symbol; CL → "CL" (value-preserving).

Shipped config / T1 pin flips (in `tests/test_instrument_context.py`, the ONE authorized Strict-Lock edit):
9. `test_es01b_shipped_config_resolves_as_es` (replaces intended-failure pin; asserts §2 table incl. file existence).
10. `test_all_shipped_configs_resolve` (empty intended_failures).

Cosmetic sweep pins:
11. `_backup_cache_to_repo`: CL DataManager → filenames `warm_start_cache_{ts}_{reason}.parquet` / `roll_metadata_{ts}_{reason}.json` (byte-format identical to HEAD); ES DataManager → `warm_start_cache_ES_{ts}_{reason}.parquet` / `roll_metadata_ES_...`.
12. `_expected_cache_timestep`: `warm_start_cache_ES.parquet`→5m; `warm_start_cache_ES_1h.parquet`→1h; legacy `warm_start_cache.parquet`/`warm_start_cache_1h.parquet` unchanged.
13. m1 display: CL avg_cost 65,000 → entry 65.00 (unchanged vs HEAD); ES avg_cost 300,600 → 6,012.00.
14. m3: dry-run log line contains the instance symbol (ES fixture) and is byte-identical for CL; warmup `entry_crossed` HEAD behavior PINNED (raw averageCost comparison — the deliberately-excluded fix).

Wrapper census outcome: NO new tests — existing `test_build_future_contract.py` delegation pins stand as the KEEP decision's fence.

Gate: full fast suite green + the HS14B ledger parity gate re-run (precedent: every T-ticket; the live_trader/ibkr_client string+display edits must show PARITY: PASS, $0.00 delta).

---

## 7. Open questions requiring human authorization
1. **D1** — accept corrected (non-byte-identical) output for v2 `CL_*` inputs? Literal byte-identity would pin model_paths at directories proven absent (§1d). Recommended: YES.
2. **D2** — unconditional `models.<side>.symbol` emission (adds one key per side to CL outputs; only surviving model↔symbol cross-check post-T6)? Recommended: YES; D2-alt (non-CL-only) drafted if refused.
3. m2 rename + `symbol="CL"` default removal → confirm spin-off micro-ticket (not T6).
4. Warmup `entry_crossed` multiplier fix (CL behavior change) → confirm spin-off micro-ticket (not T6).
5. Severable: vm_e2e `ensemble_cfg["execution_symbol"]` one-liner (5c) — include or cut?

## 8. Constraint conflicts detected in the ticket text (resolved as follows)
- "NO live engine changes" vs routed m1/m2/m3 (which live in the engine): interpreted as no
  *behavior* change to the trade path; only CL-output-identical display/log edits admitted, and the
  two genuinely behavioral candidates (m2 rename, warmup fix) deferred. Parity gate is the fence.
- "CL generator outputs byte-identical" vs B7's own fix definition: see §4 D1/D2.
