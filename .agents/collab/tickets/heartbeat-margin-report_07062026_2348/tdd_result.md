# TDD Result — heartbeat-margin-report_07062026_2348

**Status:** ✅ IMPLEMENTED & GREEN (uncommitted)

## What changed
Account-wide margin now rides the existing cached `accountValues()` feed into the
1-Hour heartbeat — no new IBKR network call, safe from the heartbeat daemon thread.

### Source
- `src/live_execution/ibkr_client.py` — `get_account_summary()`:
  - Added dict keys `init_margin_req`, `maint_margin_req`, `excess_liquidity` (default `0.0`).
  - Parse tags `InitMarginReq` / `MaintMarginReq` / `ExcessLiquidity` (`currency == "USD"`)
    in the existing `for av in acct_values` loop; docstring updated.
- `src/live_execution/live_trader.py` — `_build_heartbeat_payload()`:
  - New locals `init_margin` / `maint_margin` / `excess_liq` (init `0.0`, so the
    disconnected path still renders), extracted via `acct.get(...)`.
  - Three lines rendered directly under `Total Liq`, labelled `(acct)` to signal they
    are account-wide (span all fleet symbols on the shared gateway).

Rendered block:
```
*Account Balance:*
Total Liq: `$1,483,258.15`
Init Margin (acct): `$18,150.00`
Maint Margin (acct): `$16,500.00`
Free Cushion (Excess Liq): `$1,466,758.15`
```

### Tests (all green)
- `tests/test_account_summary.py`: `test_parses_account_margin_tags`,
  `test_missing_margin_tags_default_zero` (absent tags → `0.0`, no raise).
- `tests/test_heartbeat_margin.py` (new): renders the 3 lines + `$#,##0.00` formatting,
  ordering after `Total Liq`, and `$0.00` on the disconnected path.
- Red-first confirmed (5 failed pre-impl → 16 passed post-impl).

## Regression check
Combined run of test_live_trader_bugs / test_commission_capture / test_live_macro_refresh /
test_config_generator_symbols / test_fleet_health: 8 pre-existing failures
(4 ES01B Sortino-swap sentinels + 4 TestCosmetics cross-file pollution). **Proven
pre-existing** — the identical 8 fail with this ticket's source edits stashed; both
groups pass in isolation. This change adds zero new failures.

## Out of scope (recorded follow-up)
True per-symbol margin via `ib.whatIfOrder()` (main-thread + cache) — deferred per user
decision 2026-07-06.
