# Ticket Resolution Blueprint — es-config-drift-repin_07062026_2124
**Ticket Directory:** `.agents/collab/tickets/es-config-drift-repin_07062026_2124/`

## Bug Summary
Commit `a7a0b7d` (HEAD) swapped the fleet's ES model: it **deleted**
`configs/strategies/ES01B_Sharpe_E03_07042026.json` and added
`configs/strategies/ES01B_Sortino_E01_07062026.json`, updating
`configs/fleet/fleet_manifest.json` to reference the new one — but did **not**
evolve the sentinel-pin tests. Three states now disagree:

| Source | ES model |
|---|---|
| Running fleet child (PID 67540, in-memory) | `Sharpe_E03` (file deleted) |
| Manifest + disk (HEAD) | `Sortino_E01` |
| Sentinel tests | pin deleted `Sharpe_E03` → **12 failing tests at HEAD** |

**Root cause:** a shipped-config swap committed without evolving its sentinel pins
in the same change — the exact "336d29f lesson" the /add-remove-fleet-model gate
(d) exists to prevent, repeated.

**USER DECISION (2026-07-06):** KEEP `ES01B_Sortino_E01_07062026` — the Sortino
pick was intentional and is the best performer. Re-pin tests to it; do NOT revert.
(Note for provenance only: this overrides the general "Sharpe over Sortino" default
for ES specifically, on measured performance.)

## Target Files
- `tests/test_hourly_only_equity_session.py` — `TestES01BFlagPatch` (3 tests).
- `tests/test_config_generator_symbols.py` — `TestES01BPatchedConfig` (4 tests),
  `TestCosmetics` (4 tests).
- `tests/test_instrument_context.py` — `TestShippedConfigs::test_es01b_shipped_config_resolves_as_es`.
- (Reference only) `configs/strategies/ES01B_Sortino_E01_07062026.json`,
  `configs/fleet/fleet_manifest.json`.

## Required Changes
1. **Re-pin every sentinel** that references the deleted
   `ES01B_Sharpe_E03_07042026.json` to the shipped
   `ES01B_Sortino_E01_07062026.json`. Read the Sortino config's ACTUAL values
   (execution_symbol/exchange/tick, `enable_5m_stream` presence, T6 sentinel
   fields, pnl-display multiplier, dry-run log naming) and update the audit-table
   expectations to match — do NOT weaken any assertion; they must still enforce
   symbol resolution, session shape, T6 stamping, and cosmetics.
2. **`test_referenced_artifacts_exist_on_disk`** must pass against Sortino_E01's
   model paths (present). If it also asserts `predictions_path` existence,
   coordinate with `predictions-path-provenance_07062026_2124` (that ticket fixes
   the ES predictions path) so the two changes are consistent — do not assert a
   path the other ticket is simultaneously rewriting.
3. All 12 currently-failing tests go green with the pins pointing at Sortino_E01.
4. **Operational reconciliation (document; not code):**
   - The running ES child holds the deleted Sharpe_E03 in memory → **crash-loop
     risk** if that child restarts under the current runner (config file gone).
   - Restarting the fleet converges ES to Sortino_E01 (intended).
   - **Before restart, check ES for an open position** — the model swap will NOT
     adopt an existing Sharpe_E03 position; its GTC bracket stays at IBKR.
5. **Process reaffirmation:** call out in the PR that a shipped-config swap MUST
   evolve its sentinel pins in the same commit (add-remove-fleet-model gate d).

## Dependencies / Coordination
- Touches the same test file as `predictions-path-provenance_07062026_2124`
  (`test_config_generator_symbols.py`). Sequence or share a branch to avoid
  conflicting edits to the ES predictions assertion.

## Note on severity
This is a **committed regression** (12 tests red at HEAD). Per /ticket-manager
Step 2, regressions should not be fast-tracked — consider an Impact-Reviewer pass
before /tdd-manager, even though the fix is mechanical and the direction is decided.
