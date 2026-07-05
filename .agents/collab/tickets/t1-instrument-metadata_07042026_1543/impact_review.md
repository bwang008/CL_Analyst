# Impact Review — T1 Instrument metadata plumbing
**Ticket:** `t1-instrument-metadata_07042026_1543`
**Reviewer:** Ticket-Impact-Reviewer | **Date:** 2026-07-04 | **Branch:** `development`
**Proposal reviewed:** `audit.md` (Ticket-Auditor, 2026-07-04 15:54)

## VERDICT: APPROVE (conditional — 4 conditions below, none blocking redesign)

---

## 1. Constraint evaluation (workflow rules)

### Interface Rule — TRIGGERED, justification ACCEPTED
Two interface-adjacent changes:
- `Instrument` dataclass gains ~11 required fields. Independently verified: `Instrument(` is constructed **only** inside the registry literal in `src/core/instrument_master.py` (repo-wide grep; the sole other hit is the audit doc itself). External users import the class for type hints only (`scripts/download_macro_data.py:85-97` — attribute access on `.volatility_index`) or call `get_instrument()` (signature unchanged). Additive fields cannot break attribute access. No code iterates `INSTRUMENT_REGISTRY` outside `instrument_master.py` (verified — the symbol appears in no other .py file).
- `LiveTrader.__init__` behavior changes for configs missing `execution_symbol` (silent CL default → hard raise). Signature unchanged. Justification (house rule: no silent null defaults; M1 = latent wrong-instrument trading) is strong, alternatives were considered (§3b grandfathering rejected with sound reasoning), and the migration burden is one line in one config. Accepted.

### Base Class Rule — TRIGGERED (core utility), justification ACCEPTED
`instrument_master.py` is imported by 7 modules (gcp/vm_e2e_pipeline, gcp/orchestrator, src/data_processor, src/features/macro_features, src/config/schemas, scripts/download_macro_data, tests). All access existing attributes or call `get_instrument()`; the change is strictly additive with a CL regression-pin test (proposed test 4). The alternative (a parallel live-metadata dict) would fork the instrument source of truth — worse. Accepted.

### Refactor Veto — NOT TRIGGERED
No component is rewritten. Blast pattern: 1 core file extended additively, 1 new leaf module, 3-line change in `live_trader.py` (attribute name/type preserved), ~4-line fail-fast insertion in `cli.py`, 1-line config migration, 1 test-fixture update. Consumers of `self._execution_symbol` are untouched. This is a localized multi-file change, not a multi-component refactor. Human authorization for the ticket as a whole is NOT required; the audit's §8 human-ack items are decision flags for the Ticket-Manager/user, not architectural escalations (see §4 below).

## 2. Independent verification of riskiest claims

| # | Claim | Result |
|---|---|---|
| V1 | `self._execution_symbol` consumers untouched / CL behavior-identical | **CONFIRMED.** 49 usages in `live_trader.py` (lines 276-3989), all read-only consumers of the string attribute except the single assignment at :276-278. Proposal 4.3 preserves name and type (`str`). All 19 CL configs pass resolver rules 1-2; model cross-check: HS14B → `E2E_CL_*` → match; all other CL configs carry no derivable tag → skipped (census in audit §3a spot-checked against `configs/strategies/`). No CL config can fail. |
| V2 | Exactly one config lacks `execution_symbol`; others all "CL" | **CONFIRMED.** `configs/strategies/` holds exactly 20 JSONs; grep finds `"execution_symbol": "CL"` in 19; `ensemble2_opt.json` has 0 occurrences. Additionally checked `models/registry/**/*.json` strategy-shaped configs (9 files incl. production_output/canary_output strategy_config.json and lab/optimized_ensemble_cfg.json): **all 9 HAVE the field** — no hidden breakage if those are ever passed to the CLI or parity harness. |
| V3 | MCL registry entry preserves Two-Stream | **CONFIRMED.** `ibkr_data_feed.py:91-96,133-138`: continuous ("brain") subscription branches `symbol == "MCL"` → `build_mcl_contract`, keyed off `self._execution_symbol` — T1 does not touch this file or the subscription call, so an MCL config still gets an MCL-continuous brain stream exactly as today. The `micro_of` → brain=CL mapping is consumed only by the model-tag validator (MCL + `E2E_CL_*` passes, matching the CL-trained-models reality). Registry addition itself is inert for running code: today `get_instrument("MCL")` is never called on the live path (raises if it were). |
| V4 | Making `execution_symbol` required can't break non-cli constructions | **CONFIRMED with one audit gap.** Repo-wide grep: `LiveTrader(` construction sites are exactly 5 — `cli.py:279`, `tests/test_live_macro_refresh.py:31,41,67`, and **`scripts/livetest_engine.py:711`** (the parity/sim harness — **omitted from audit §3e**). livetest_engine builds its strategy from a real `--config` JSON (`:604-630`), so post-migration all shipped configs pass; no code change needed there, but see condition C3. The 3 test constructions use `DummyStrategy.config = {}` and WILL break — audit already plans the fixture fix (§3e/test 16). All other live tests bypass `__init__` and assign `trader._execution_symbol = "CL"` directly (verified: 17 assignment sites across 10 test files) — unaffected. Backtest engine / optimizer / fleet_runner never read `execution_symbol` (repo grep: within `src/` only `live_trader.py` reads it) and never construct LiveTrader. Legacy `--strategy` CLI path is dead: `_STRATEGY_REGISTRY = {}` (`live_trader.py:110`) and `cli.py:209` errors without `--config`. |
| V5 | `Instrument` extension breaks nothing training-side | **CONFIRMED.** `src/data/databento_data_builder.py` does not import instrument_master at all (its `instrument_id` hits are Databento columns). `scripts/download_macro_data.py` uses `Instrument` as a type hint + `.volatility_index`. `gcp/orchestrator.py`, `gcp/vm_e2e_pipeline.py:1135-1137`, `src/data_processor.py:3198`, `src/features/macro_features.py:126`, `src/config/schemas.py:195-197` all go through `get_instrument()` attribute access — additive fields are invisible to them. Side effect noted: `schemas.py` manifest validation will newly accept MCL/MES/MNQ/MGC/SIL as manifest symbols — more permissive, not breaking (and arguably correct). `tests/test_schemas.py:9-16` asserts CL tick_size + unknown-symbol raise — both preserved. |
| V6 | PA tick correction blast radius | **CONFIRMED ~ZERO.** The only computational consumer of `tick_size`/`slippage_ticks` is `gcp/vm_e2e_pipeline.py:1137` (slippage = ticks × tick_size at pipeline runtime). No PA manifest exists (`configs/*pa*` → none), no PA pipeline has ever run, no test asserts PA values, no stored artifact embeds them retroactively. The existing entry (0.05/$5.00 with a self-doubting comment at `instrument_master.py:59`) is wrong per NYMEX spec (100 oz × $0.10 = $10.00 — auditor's values check out arithmetically and against the 100 multiplier already assumed in the existing comment). Correcting it in T1 is safe; I endorse inclusion rather than a KNOWN-WRONG comment. |

Also verified: no import cycle for the new module (`instrument_master.py` imports stdlib only — lines 1-2); `strategy.config` exists at the proposed cli.py insertion point (used at `cli.py:202`); the proposed insertion sits before `DataFeedFactory.create` at `cli.py:276` as claimed.

## 3. Conditions of approval

- **C1 (must-fix in design): micro entries are missing required-field values.** The per-symbol table (audit §4.1) omits `cftc_code` and `volatility_index` for MCL/MES/MNQ/MGC/SIL — both are existing required fields of `Instrument`. Specify explicitly: micros inherit the parent's `cftc_code` and `volatility_index` (with a comment that micros are execution-only and must never enter training pipelines), and extend `test_micro_entries_consistent` (test 3) to pin cftc_code/volatility_index equal to parent.
- **C2: preserve case normalization.** Current code does `.get(...).upper()`. `resolve_instrument_context` must return the upper-cased symbol (and `get_instrument` already upper-cases lookup), so a lowercase `"cl"` config remains accepted exactly as today. Add a resolver test for lowercase input.
- **C3: correct §3e and validate the parity harness.** `scripts/livetest_engine.py:711` is a fifth LiveTrader construction site (the ledger-parity/sim harness). No code change is required, but because `LiveTrader.__init__` gains a new raise path, the ledger parity gate (HS14B via livetest_engine) must be re-run as part of T1 validation to keep the 2026-07-04 PARITY PASS baseline honest.
- **C4: keep the intended-failure documentation.** Test 15 (`ES01B_Sharpe_E03_07042026.json` RAISES as-is) must land with the change, and ticket_status must carry forward the two human-visible flags (ES01B refuses to start until T6 regenerates it; PA correction included). GVZ-on-IBKR (audit §8.3) and session-hours authority (§8.4) are T4/T5 concerns — correctly deferred, no T1 action.

## 4. Disposition of audit §8 human-ack items

1. PA tick fix → verified zero-consumer (V6); **approve inclusion in T1**.
2. ES01B intended startup failure → this is the ticket's stated purpose (M1); acceptable sequencing provided C4's test + status flag land. Ticket-Manager should surface it to the user before T1 deploys to any host that runs ES01B.
3. GVZ IBKR availability → T4 runtime concern; registry string is inert until then. No block.
4. Session-hours consumers → verified none exist today (`session_hours_ct` is a new field; nothing reads it until T5). No block.
5. T6 `models.*.symbol` emission → endorse; the validator's forward-compat hook (test 14) is the right shape.

## 5. Summary
Localized, additive, well-fenced. The zero-behavior-change proof for CL holds under independent verification; the one omission found (livetest_engine construction site) is benign but must be acknowledged and covered by a parity re-run (C3). Approve for hand-off to the TDD tester/implementer with conditions C1-C4.
