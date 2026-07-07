# Ticket Resolution Blueprint — heartbeat-margin-report_07062026_2348
**Ticket Directory:** `.agents/collab/tickets/heartbeat-margin-report_07062026_2348/`

> **Type:** Feature (additive logging) — **NOT a bug.** Low severity, non-regression.
> Fast-tracked per ticket-manager Step 2 (low severity + no recent regression → Impact-Reviewer skipped).

## Feature Summary
Add account **margin** reporting to the 1-Hour Telegram heartbeat so the operator can
see, at a glance, how much margin the account is committing to hold its futures
positions and how much free cash/cushion remains before a margin call. This runs in a
**fleet** (`fleet_runner.py` launches one `live_trader.py` child per symbol, all sharing
the **same IB Gateway → same account**), so the margin numbers are **account-wide** and
will be identical across every instance's heartbeat.

### Investigation result (feasibility — confirmed)
- `ib_insync` 0.9.86 (`trader` env). The heartbeat already reads `NetLiquidation` and
  `AvailableFunds` from `self.ib.accountValues()` — a **cached** property used
  deliberately (see [`ibkr_client.py:783`](../../../../src/live_execution/ibkr_client.py))
  because the blocking `accountSummary()` crashes when called from the heartbeat daemon
  thread (no asyncio loop). The **account-wide margin tags ride the same already-streamed
  feed**, so no new network call and no thread-safety risk.
- `PortfolioItem._fields` has **no margin field**, so IBKR does **not** expose per-position
  margin in the portfolio/account feed. A true per-symbol split would require a blocking
  `ib.whatIfOrder()` on the main event-loop thread + caching. **Per user decision
  (2026-07-06): NOT in scope for this ticket** — report account-wide margin only.

## Target Files
- `src/live_execution/ibkr_client.py` — `get_account_summary()` (~line 751–804)
- `src/live_execution/live_trader.py` — `_build_heartbeat_payload()` (~line 671–747)
- `tests/live_execution/` — add/extend the unit test covering `get_account_summary`
  parsing and the heartbeat payload string (mirror existing heartbeat/account-summary tests).

## Required Changes

### 1. `ibkr_client.get_account_summary()` — surface account-wide margin tags
Extend the returned `summary` dict with three new keys, defaulting to `0.0`:
- `init_margin_req` — initial margin to open/hold (account-wide)
- `maint_margin_req` — maintenance margin to hold (account-wide)
- `excess_liquidity` — free-cash cushion before a margin call (account-wide)

In the existing `for av in acct_values:` loop, add branches that match these IBKR
account-update tags (same `currency == "USD"` guard the existing `NetLiquidation`/
`AvailableFunds` branches use):
- tag `"InitMarginReq"`   → `summary["init_margin_req"]`
- tag `"MaintMarginReq"`  → `summary["maint_margin_req"]`
- tag `"ExcessLiquidity"` → `summary["excess_liquidity"]`

Notes for the implementer:
- **Do NOT add a new network request.** Read only from the already-fetched
  `self.ib.accountValues()` list — this is what keeps it safe from the heartbeat thread.
- Keep the `float(av.value)` conversion consistent with the existing branches.
- Follow the project rule (no silent null defaults for *config*): these are **live
  broker values**, so a `0.0` default when the tag is absent is acceptable and matches
  the existing `net_liquidation`/`available_funds` behavior — do not invent config
  fields. Do not raise if a tag is missing (a flat/just-connected account legitimately
  reports no margin tags yet).
- Update the method docstring to list the three new keys.
- **Verification caveat for the coder:** on some IBKR accounts the margin tags arrive
  under `currency == "USD"`; confirm against the paper/live account. If a tag is only
  present under a non-USD `currency` (e.g. `"BASE"`), match tag name first and accept the
  base-currency value rather than dropping it — but keep the `USD` preference consistent
  with the existing two branches. Document whichever is observed.

### 2. `live_trader._build_heartbeat_payload()` — read + render the new fields
- Where the method already pulls `acct = self.exec_client.get_account_summary(...)` and
  extracts `net_liquidation`, also extract the three new keys with `acct.get(..., 0.0)`
  (keep the existing `try/except` guard and the "only if connected" guard intact).
- Initialize three new locals (`init_margin`, `maint_margin`, `excess_liq`) to `0.0`
  alongside the existing `net_liq = 0.0`, so the "not connected" path still renders.
- Extend the `*Account Balance:*` block so the lines appear **immediately after
  `Total Liq`**, exactly as the user requested. Target rendering:

  ```
  *Account Balance:*
  Total Liq: `$1,483,258.15`
  Init Margin (acct): `$18,150.00`
  Maint Margin (acct): `$16,500.00`
  Free Cushion (Excess Liq): `$1,466,758.15`
  ```

  - Use the same ``$`{value:,.2f}`` `` formatting as the existing `Total Liq` line.
  - Label the margin lines explicitly as **account-wide** (e.g. `(acct)`), since in a
    multi-symbol fleet these span every instance's positions — this prevents the operator
    from mis-reading them as this-symbol-only. Do **not** add a per-symbol margin line
    (out of scope; would be misleading without the whatIf path).
  - `Excess Liquidity` is the "how much free cash needs to be in the account" number the
    user asked about; keep it in this block.

### 3. Tests
- Extend the `get_account_summary` test with fake `accountValues()` entries for
  `InitMarginReq` / `MaintMarginReq` / `ExcessLiquidity` and assert the three new keys are
  parsed. Add a case where the tags are **absent** and assert they default to `0.0`
  (no raise).
- Extend the heartbeat-payload test to assert the three new lines render in the
  `*Account Balance:*` block, directly after `Total Liq`, with correct `$#,##0.00`
  formatting — and that the "not connected" path still renders them as `$0.00`.

## Out of Scope (explicit)
- True per-symbol / per-contract margin via `ib.whatIfOrder()` (main-thread + cache).
  Recorded as a possible follow-up ticket if the operator later wants the per-symbol split.
- Any new IBKR subscription or network call from the heartbeat thread.
