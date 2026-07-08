# Impact Review — exit-triggers-eod-oppsignal_07072026_1924
**Reviewer:** TICKET-IMPACT-REVIEWER
**Date:** 2026-07-07 19:30
**Blueprint reviewed:** `.agents/collab/tickets/exit-triggers-eod-oppsignal_07072026_1924/blueprint.md`
**Verdict:** APPROVE (with one binding implementation condition, see §5.1)

---

## 1. Scope of review

Feature ticket, 2 phases, backtester-only, default-off. Reviewed for blast radius
exactly as a fix proposal. All blueprint claims were cross-referenced against the
working tree (which already contains the uncommitted, tested `weekend_flatten`
overlay this ticket builds on).

Files cross-checked:
- `agent/backtest_engine.py` (weekend overlay: `__init__` kw-only param L321,
  `from_config` routing L419, `_weekend_flatten_triggered` L683–706, fire sites
  L769/L924, precompute L1060–1074, renderer lists L1591/L1752)
- `src/live_execution/strategy_config.py` (`WeekendFlattenConfig` L82,
  `parse_weekend_flatten` L107 with loud-raise on enabled-without-profit_atr_mult,
  `StrategyConfig.weekend_flatten` L169, `_DEFAULT_WEEKEND_MIN_GAP_HOURS = 40.0` L104)
- `src/live_execution/strategies/execution_models.py` (`EngineState` L38–51 — confirmed
  no entry price / floating PnL; `TieredEnsembleStrategy.VALID_CONFLICT_MODES` L499;
  conflict block L762–806; deprecated `JointPortfolioStrategy` has a SEPARATE
  `VALID_CONFLICT_MODES` L1111 — untouched per blueprint)
- Consumers: `agent/strategy_optimizer.py`, `agent/generate_ensemble_artifacts.py`,
  `src/live_execution/strategies/configurable_strategy.py`,
  `src/live_execution/live_trader.py`, `scripts/ledger_parity_check.py`,
  `scripts/trade_reconciler.py`, `agent/backtest_cl_concurrent.py`, tests.

All blueprint factual claims about code locations and shapes verified accurate,
with ONE imprecision (§4.1) whose practical consequence is nonetheless as the
blueprint states.

## 2. Blast-radius map

### Phase 1 — `eod_flatten`
| Change | Blast radius | Assessment |
|---|---|---|
| New `EodFlatten` parse + `StrategyConfig` field (default None) | `StrategyConfig` consumers: backtest_engine, live_trader, backtest_cl_concurrent, 2 test files | Additive defaulted dataclass field; mirrors proven weekend pattern; no consumer break |
| `BacktestEngine.__init__` new optional param | All constructor call sites | `__init__` is keyword-only (`*` at L301) — additive optional kwarg cannot break any call site |
| New `ExitReason.EOD_FLATTEN` | Enum consumers: renderer lists (hard-coded, blueprint updates both), `scripts/trade_reconciler.py` (`_normalize_exit_reason` passes unknown strings through — safe), `scripts/ledger_parity_check.py` (see §5.3) | Additive enum member; no exhaustive-match consumer found |
| Generalize `_weekend_flatten_triggered` return | PRIVATE helper; referenced only in `agent/backtest_engine.py` and the ticket's own `tests/test_weekend_flatten.py` (extended by this ticket) | Intra-module; not a cross-module interface |
| Disjoint gap bands `[eod.min_gap, weekend_threshold)` vs `[weekend.min_gap, ∞)` | A/B attribution only | Sound; sets disjoint by construction; weekend precompute untouched |

### Phase 2 — `close_existing_position_if_profit`
| Change | Blast radius | Assessment |
|---|---|---|
| `EngineState` + 2 optional defaulted fields | 3 construction sites: engine (single reused instance), `configurable_strategy.py:459` (explicit kwargs), tests | All fields already defaulted; appending defaulted fields breaks nothing; existing strategies ignore them |
| `VALID_CONFLICT_MODES` tuple + 1 entry | Mode-string consumers: `strategy_optimizer.py:1168–1171` hard-codes its OWN 3-mode categorical (NOT read from the tuple) → search space untouched, consistent with no-optimizer-crutch house rule; `generate_ensemble_artifacts.py:412–414` passes strings through with "hold" fallback — tolerant; `_reapply_strategy_level_params` passes any mode string unmodified | Additive; existing 3 modes byte-identical per blueprint test plan |
| New conflict branch (EXIT via existing SIGNAL_EXIT plumbing) | `_run_single_strategy` L1298–1301 EXIT handling already exists; no new engine exit path | Additive branch; both-firing→HOLD and losing→HOLD are conservative |
| Price-basis fix (engine publishes exec-basis PnL) | Correctness-critical design decision | CONFIRMED necessary: `on_bar` receives brain (adjusted) close; `_entry_fill` is exec (raw) basis. Strategy-side computation would be wrong. This is the localized-correct design, not scope creep |

## 3. Three-rule evaluation

**1. Interface Rule — triggered in weakest (additive) form; justification accepted.**
`EngineState` is shared across backtest and live modules, so adding fields touches a
cross-module structure. But: every existing field is defaulted, the two new fields are
defaulted-None appended fields, and the sole live construction site
(`configurable_strategy.py:459`) uses explicit kwargs — zero call-site breakage.
Business justification is strong and specific: the price-basis trap proves the only
alternative (strategy-side computation from brain close) is WRONG, not merely less
convenient. `BacktestEngine.__init__` is keyword-only, so its new param is
break-proof. Accepted under the Business Justification exception.

**2. Base Class Rule — not triggered.**
`BaseExecutionStrategy` ABC untouched (verified). Only the concrete
`TieredEnsembleStrategy` mode tuple is extended; the deprecated
`JointPortfolioStrategy` has its own separate tuple (L1111) and is untouched.
`EngineState` is a core shared structure but the change is additive-defaulted only
(covered above).

**3. Refactor Veto — not triggered.**
No component is rewritten. Every change is an additive branch, additive enum member,
additive config block, or extended tuple — all behind default-off config with a
byte-identical no-op requirement for existing configs, enforced by the blueprint's own
test gates. The only genuinely cross-cutting change (live wiring) is explicitly
EXCLUDED and deferred behind a human gate — which is precisely what the Mandatory
Human Authorization guardrail exists for. No human authorization needed for THIS
ticket as scoped.

## 4. Blueprint imprecision found (consequence unchanged)

### 4.1 Live DOES route through `on_bar` — but EXIT is a dead path live
Blueprint says "Live `_on_new_bar` does NOT route through `strategy.on_bar()`."
Imprecise: `live_trader.py:4217` calls `ConfigurableStrategy.evaluate`, which calls
`self._exec_strategy.on_bar(...)` at `configurable_strategy.py:471` with a hand-built
`EngineState` (L459–469). So `TieredEnsembleStrategy`'s conflict block DOES execute
live. However the practical conclusion stands, twice over:
1. Live's `EngineState` never populates `floating_pnl_points` → `profitable` is False
   → the new mode's branch can never emit EXIT live;
2. Even if it did, `live_trader.py:4262–4265` treats `action=="EXIT"` as a dead path
   ("bracket-only exits") — it zeroes the virtual ledger, it does not close positions.

Net: no live order can be produced by this change. But see §5.1.

## 5. Conditions and notes for the implementer

### 5.1 BINDING CONDITION — loud guard on the None-PnL path (house rule: no silent null defaults)
Because the new mode WILL execute live if a config ever carries it (per §4.1), and
live never populates `floating_pnl_points`, the mode would silently degrade to
permanent "hold" semantics live — a silent backtest-vs-live divergence of exactly the
class in the fleet's history (multi-symbol silent failures). Inside the new branch:
if the mode is active AND `state.position != 0` (in-position path reached) AND
`state.floating_pnl_points is None`, RAISE loudly instead of holding. None-when-flat
stays legitimate; None-while-in-position means the caller doesn't publish the field
and must not run this mode. This is 2–3 lines inside the very branch being added
(no scope change), converts the silent divergence into a crash the fleet error queue
will catch, and directly enforces the no-silent-null-defaults house rule. Add one
test pinning it.

### 5.2 Cooldown flavor for EOD_FLATTEN (note)
`_close_trade`'s exit-reason handling feeds cooldown flavoring downstream. EOD_FLATTEN
must receive the identical treatment WEEKEND_FLATTEN already receives, and a test
should pin it — otherwise the two overlay exits diverge in re-entry behavior for no
designed reason.

### 5.3 Parity-map gap is pre-existing, not new (note)
`scripts/ledger_parity_check.py` `EXIT_REASON_MAP` (L44–50) contains neither
WEEKEND_FLATTEN nor EOD_FLATTEN. Harmless while default-off (fleet configs carry
neither block), but any future parity run with an overlay enabled will mis-map. This
is a pre-existing property of the already-implemented weekend overlay; EOD adds no new
class of gap. Flag for the eventual live-parity ticket.

### 5.4 Optimizer isolation verified (note)
`strategy_optimizer.py:1168–1171` hard-codes `["hold", "close_existing_position",
"reverse_position"]` and does not read `VALID_CONFLICT_MODES` — extending the tuple
cannot leak the new mode into the search space. Blueprint's "NO search-space changes"
is structurally guaranteed, not just promised.

## 6. Decision

**APPROVE.** Both phases are additive, default-off, and pattern-cloned from an
already-tested overlay. Interface Rule touches are additive-defaulted with a
verified-necessary justification (price-basis trap). Base Class untouched. No
refactor. The one real hazard (silent live degrade of the new mode) is closed by the
binding condition in §5.1, which is in-scope and additive. Live wiring correctly
remains human-gated and out of this ticket.
