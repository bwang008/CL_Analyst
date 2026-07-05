# Ticket Resolution Blueprint — t8-workflow-doc-gates_07052026_0403
**Ticket Directory:** `.agents/collab/tickets/t8-workflow-doc-gates_07052026_0403/`

## Requirement Summary
T8 (user-requested): the workflows that guided the ES standup produced the broken
artifacts; they must gain validation gates + be updated to T1-T7 ground truth so a
future symbol standup cannot recreate the defects. DOC-ONLY (no code, no tests).
Reviewer verdict: APPROVE with required corrections R1-R3 and recommended R4-R5.
Edit plan: `audit.md` §3-§5 (exact gate texts + per-file plan); fact-check +
corrections: `impact_review.md`. This document governs on conflict.

## Manager rulings (given)
- `.agent/workflows/` twins: deprecation BANNERS, not deletion.
- The 34-manifest `defaults` retrofit is OUT of T8 (user-flagged follow-up; ZC first).
- T4-routed MacroFeatureEngine call-site sweep → its own code micro-ticket (not here).
- G7/G8 code debt deferred behind the gates; docs/prompts/ = ARCHIVE, no edits.

## Edits (apply audit §3-§5 EXACTLY, amended by R1-R5)
1. `.agents/workflows/build-symbol-pipeline.md` —
   Phase 0: full 17-field registry spec (session-shape family selection GLOBEX/GRAINS/
   EQUITY, new-shape = SDLC STOP; tick invariant; roll fields; live_vol_index; micro
   entries) + blocking gate `conda run -n trader python -m pytest
   tests/test_instrument_master_live_fields.py tests/test_instrument_context.py -q`.
   Phase 1: `{SYM}_raw_1h.parquet` live seed step + the USER RULING (all data hourly;
   NO 5m acquisition; new-symbol configs set live_config.enable_5m_stream: false).
   Phase 5: C1 warning (do NOT ship _opt_/_hybrid_ target-pairs configs for non-CL
   until the code fix; strategy_optimizer.py:1443-1447/:1868-1872) + C2 mandate
   (non-CL manifests MUST carry a `defaults` block; generator falls back to the CL
   base at generate_ensemble_artifacts.py:303 otherwise) + line-90 softness replaced
   by the hard gate reference.
   Phase 6: POST-CANARY CONFIG GATE — the §3.4 validator script (R3: zero-configs-found
   = FAIL; R4: sys.path.insert(0, os.getcwd()) + "run from repo root" note) asserting
   per config: resolve_instrument_context succeeds, execution_symbol == manifest
   baseline.symbol, models.*.symbol present, model_path + predictions_path exist.
   R1: document BOTH expectations — the preserved ES batch dir
   (batch_20260704_0701_ES_01B_SCOUT/configs) correctly FAILS the gate (pre-T6
   fixtures, kept as the negative example); the promoted
   configs/strategies/ES01B_Sharpe_E03_07042026.json PASSES.
2. `.agents/workflows/run-cloud-batch.md` — post-download config gate reference; remove
   the classifier-blocked `powershell -ExecutionPolicy Bypass` prefix (align with
   build-symbol-pipeline's rule).
3. `.agents/workflows/post-optimize.md` — gate reference + explicit C1 warning on
   Option B (it IS the target-pairs path).
4. `.agents/workflows/generate-trade-configs.md` — Step 3 "duplicate the baseline
   config" replaced with symbol-correct instructions + gate reference (this step is
   the ES01B defect pattern verbatim).
5. `.agents/workflows/run-live.md` — current CLI entry (`-m src.live_execution.cli`),
   preflight resolve check, enable_5m_stream guidance, data/exec port guidance.
6. `.agents/workflows/grab-data.md` — add ZC/ZS/SI to the symbol table; hourly-only
   ruling note.
7. `.agents/workflows/livetest.md` — R2 CORRECTED text: BacktestEngine rounds brackets
   to the PENNY grid (backtest_engine.py:659-669,793-797); ConfigurableStrategy uses
   round(x,2) (:561-565); only live order placement snaps to instrument tick
   (T3). Grid equality is CL-only; non-CL carries a documented ≤½-tick bracket-grid
   gap. Do NOT claim both engines round to tick.
8. `.agents/workflows/smoketest.md` — cache filename list per derive_data_paths (CL
   legacy exceptions + per-symbol patterns).
9. `.agents/workflows/run-cloud-experiment.md` — gate reference where configs land in
   configs/strategies/.
10. `.agents/workflows/run-vector-cloud-batch.md` — R5: deprecation banner targeting
    the RETIRED manifests/schema (hourset09 manifests gone; legacy `defaults` schema
    fails BatchSweepConfig) — NOT the -SweepMode "frictionless" mechanism (still live
    in run_sweep_batch.ps1:37/:1019).
11. `.agents/workflows/validate-parity.md` + `validate-ledger-parity.md` — NOTE-only:
    stale docstring references post-957ced7/T3 (per audit; keep minimal).
12. `.agent/workflows/` twin copies — deprecation banner at top pointing to
    `.agents/workflows/` (content untouched).
13. `docs/headless-deployment.md` + `deploy/systemd/README.md` — fleet sections gain:
    execution_symbol fail-fast note, per-symbol seed/macro prerequisites,
    enable_5m_stream: false for hourly-only symbols (matches fleet_runner.py +
    cli.py:227-229 reality).
Minor corrections from the review: G8's schemas.py = src/config/schemas.py; G6 line
spans :331-348/:395-417.

## Verification (doc-only)
- Re-run the Phase 6 gate script against the ES batch dir (expect FAIL, R1 negative
  example) AND against configs/strategies/ES01B_Sharpe_E03_07042026.json (expect PASS).
- Proofread pass: every command/path/line-number inserted must match HEAD (the
  reviewer's fact-check is the reference).
- Full fast suite must remain green (docs cannot break it, but run it as the standard
  pre-commit check).
