# Ticket Resolution Blueprint — threshold-min-prob-consolidation_07222026_1230
**Ticket Directory:** `.agents/collab/tickets/threshold-min-prob-consolidation_07222026_1230/`
**Authorization:** operator, 2026-07-22 (same "fix everything ... proceed" grant as
cooldown-single-authority-wiring_07222026_1051; this is the companion small
ticket the operator asked about: "min_prob is the only one being used now, so
would it be ok to remove the redundancy here as well?").

## Finding
For TIERED configs `models.*.threshold` is a synced informational duplicate of
`tiers[].min_prob`:
- TieredEnsembleStrategy (execution_models.py:551-567) documents it as
  "cosmetic/informational", warns on divergence; tiers control execution.
- generate_ensemble_artifacts.py:549 writes it FROM tiers[0].min_prob.
- ConfigurableStrategy tiered branch derives thresholds from tiers only.
- strategy_optimizer warm-start (tiers-first, :1122-1128) and
  prediction_parity_compare (tiers override, :251-257) already prefer tiers.
It IS load-bearing for non-tiered configs (SingleModelStrategy,
ConfigurableStrategy no-tiers ensemble branch) — those keep it.

Two silent-default landmines guarded the removal:
1. agent/sweep_ensembles.py defaulted missing threshold to 0.55 and then
   WROTE that value into every `tiers[*].min_prob` of the patched config —
   a stripped base config would have silently rewritten entry thresholds.
2. ConfigurableStrategy's no-tiers ensemble branch silently defaulted to
   0.50 per side.

## Resolution
- 5 fleet configs: `models.long/short.threshold` removed (values were exact
  duplicates of the side's tier min_prob in every config — verified).
- agent/sweep_ensembles.py: new `_resolve_base_threshold(cfg, side)` —
  tiers[].min_prob canonical (min across tiers), models.<side>.threshold
  fallback for non-tiered, ValueError when neither resolves. All three 0.55
  default sites replaced (frictionless, config-patch, legacy display).
- ConfigurableStrategy no-tiers ensemble branch: models.<side>.threshold
  REQUIRED for a side present in `models` (ValueError before any model I/O);
  side absent from `models` = fail-closed 1.0 sentinel (mirrors tiered
  branch). Silent 0.50 default eliminated (no-silent-null-defaults).
- generate_ensemble_artifacts sync-write KEPT deliberately: generated batch
  artifacts stay self-describing, and the TieredEnsemble divergence warning
  guards drift. Changing generated-config shape is pipeline surface and was
  consciously excluded from this small ticket.
- tests/test_threshold_consolidation.py: 11 tests — resolution precedence,
  loud-failure paths, all-5-fleet-configs resolve from tiers alone, and a
  hygiene scan pinning the removed keys of BOTH consolidation tickets
  (models.*.threshold, sl/tp_cooldown_bars) plus per-side cooldown_bars
  presence.

## Notes
- prediction_parity_compare.py left unchanged (already tiers-first; its 0.5
  default only feeds non-tiered configs which keep the key).
- agent/sweep_ensembles.py is pipeline-adjacent tooling: per the standing
  canary rule, next scout/prod batch after this change should run behind the
  usual canary (no batch was in flight during this edit).
