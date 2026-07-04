# Ticket Resolution Blueprint — multi-config-fleet-runner_07042026_0646
**Ticket Directory:** `.agents/collab/tickets/multi-config-fleet-runner_07042026_0646/`

## Requirement Summary
Deploy N trained models (one strategy JSON each, e.g. `configs/strategies/HS14B_Sharpe_E01_06262026.json`)
onto the live system simultaneously, without breaking the existing single-strategy
`live_trader.py` code path or the training pipeline, and without violating IBKR API limits.

**Decision: keep one OS process per config (the current model), and add a thin
"fleet runner" supervisor that launches/monitors all instances from a single manifest.
Do NOT refactor LiveTrader into a multi-strategy single process.**

### Why process-per-config (not multi-config in one process)
- `LiveTrader` is a single-position finite-state machine: ~50 mutable per-trade state
  fields (`_position_side`, `_entry_price`, `_tp_order_ids`, `_sl_order_id`,
  `_trailing_activated`, software-OCA sets, etc.), one telemetry DB, one execution
  symbol, one recovery/ledger path. Multiplexing strategies inside it is a large,
  high-regression-risk refactor against a system that just passed the parity gate.
- Isolation already exists and is battle-tested: per-config `live_config.client_id`,
  per-cid telemetry DB (`live_telemetry_cid{N}.db`), per-cid log files, shared
  warm-start OHLCV cache (cli.py:225-260).
- Fault isolation: one bad config/model crash cannot take down the other strategies.
- Cloud alignment: process-per-config maps 1:1 to one container per strategy
  (docker-compose locally → Cloud Run / GCE containers later). The fleet runner is a
  local stand-in for the cloud orchestrator, so nothing is throwaway.

### IBKR budget per instance (verified in code)
- 2 TCP connections: data feed at `client_id`, execution at `client_id + 1` (cli.py:276-277).
- Steady state: 2–3 `keepUpToDate` streaming bar subscriptions (5m continuous "brain",
  1h continuous if `bar_size: 1h`, front-month 5m "hands"). Push-based — no polling requests.
- Startup burst only: warm-start backfill chunks (5m + 1h), VIX/OVX daily-close fetches,
  `reqContractDetails` front-month resolution, contract qualification ≈ 5–10 historical
  requests per instance.
- Orders: a handful per hour worst case (hourly models) — negligible vs 50 msg/s.

### IBKR limits (documented)
- 50 API messages/sec inbound to TWS/Gateway.
- Historical data pacing (error 162, already handled with backoff in
  `ibkr_client.py:_request_historical_data`): no identical request within 15 s,
  ≤6 requests per 2 s for the same contract, ~60 requests per rolling 10 min
  (soft-throttled for ≥1-min bars); ≤50 simultaneous open historical requests.
- ~100 concurrent streaming market data lines (base allowance).
- 32 simultaneous API client connections per TWS/Gateway instance.

Conclusion: at 5–15 strategies we are safely inside every limit **except** the startup
burst — launching all instances at the same second can trip the 15-s identical-request
rule (two CL strategies backfilling the same contract) and the 10-min budget.
The fleet runner therefore MUST stagger instance startup.

## Existing WSL Deployment (must integrate, not duplicate)
The live bot currently auto-starts on WSL boot via systemd (WSL2 Ubuntu, systemd
enabled), documented in `docs/headless-deployment.md` and `deploy/systemd/README.md`:
- `ibc-gateway.service` — IBC + headless IB Gateway (Xvfb) on paper port 4002.
  **Unchanged by this ticket.**
- `live-trader.service` — single instance, hardcoded
  `--config configs/strategies/HS09_Ensemble_E01_06032026.json`, `Restart=always`,
  running from the WSL clone `/home/bwang008/projects/CL_Analyst`.
  **Replaced by this ticket** with `fleet-runner.service`.

Integration plan:
- New unit `deploy/systemd/fleet-runner.service`: same skeleton as live-trader.service
  (After/Requires ibc-gateway, EnvironmentFile, ExecStartPre port-4002 wait, journald),
  but ExecStart runs `python -m src.live_execution.fleet_runner --manifest
  configs/fleet/fleet_manifest.json`. Division of labor: systemd restarts the *runner*;
  the runner restarts crashed *children* (children must NOT also be systemd units).
- Migration on WSL: `sudo systemctl disable --now live-trader.service` before enabling
  fleet-runner.service — the systemd README explicitly warns duplicate trader processes
  collide on IBKR client IDs.
- TimeoutStopSec must grow (e.g. 90s): the runner needs to SIGTERM all children and
  wait for their cache-save/disconnect before systemd SIGKILLs.

Verified WSL state (inspected live 2026-07-04):
- **Both `ibc-gateway.service` and `live-trader.service` are DISABLED and inactive.**
  No autostart hooks exist anywhere (`~/.bashrc`, `~/.profile`, `/etc/wsl.conf` has
  only `[boot] systemd=true`). The bot does NOT currently auto-start on WSL boot —
  starts have been manual (`systemctl start`). Consequence: no conflict risk during
  migration, but "enable on boot" must be part of this ticket if autostart is wanted:
  `sudo systemctl enable ibc-gateway.service fleet-runner.service`.
- Deployed unit `/etc/systemd/system/live-trader.service` matches the repo copy
  (description says hourly_ensemble_009, config still HS09) and passes `--port 4002`.
  The WSL clone (`~/projects/CL_Analyst`, branch development) is **42 commits behind
  origin/development** at 3a11184 — its old cli accepts `--port`, but after the
  required `git pull` the flag disappears, so the old unit would crash-loop.
  The fleet-runner.service replacement resolves this; delete or leave-disabled the
  old unit.
- Clone workspace: untracked `monitor_wsl.sh` (journalctl tail helper, harmless)
  and empty `nohup.out` (stale). Env: Python 3.12.13, pandas 2.3.3 — no pandas
  1.5.3 concern on WSL (that applies to the local Windows conda env only).
- Single paper gateway on 4002 (nothing on 4001/7496/7497). The fleet manifest must
  set BOTH `data_port: 4002` and `exec_port: 4002` — cli.py's defaults are 4001
  (live gw) for data / 4002 for exec, wrong for this single-gateway box.
- Single gateway means all 2N connections land on one gateway: N ≤ 16 instances.

Migration runbook (WSL):
1. `git pull origin development` in `~/projects/CL_Analyst` (brings fleet runner +
   the 42 pending commits incl. live trailing-stop and telemetry fixes).
2. `pip install -r requirements.txt` in the trader env if deps changed.
3. Copy `deploy/systemd/fleet-runner.service` → `/etc/systemd/system/`,
   `daemon-reload`, `enable ibc-gateway fleet-runner`, `start`.
4. Leave `live-trader.service` disabled (or `rm` + `daemon-reload`).
5. Smoke test: fleet runner with a 1-instance manifest (HS14B) in `--dry-run`,
   verify staggered second instance, then flip dry-run off.

## Target Files
- `configs/fleet/fleet_manifest.json` (NEW) — list of strategy config paths + optional
  per-instance overrides (ports, dry_run).
- `src/live_execution/fleet_runner.py` (NEW) — supervisor CLI.
- `deploy/systemd/fleet-runner.service` (NEW) — systemd unit replacing live-trader.service.
- `deploy/systemd/README.md` + `docs/headless-deployment.md` — update service install and
  code-deployment sections (pkill target becomes `fleet_runner`, migration steps).
- `src/live_execution/cli.py` — no behavioral change required; fleet runner shells out to
  `python -m src.live_execution.cli --config <path>` per entry.
- `src/live_execution/ibkr_client.py` — (optional, low priority) fix misleading comment:
  client IDs are arbitrary int32s; the "32" limit is concurrent connections, not ID range 0-31.

## Required Changes
1. **Fleet manifest schema** (`configs/fleet/fleet_manifest.json`):
   `{ "instances": [ { "config": "configs/strategies/HS14B_Sharpe_E01_06262026.json",
   "enabled": true, "extra_args": [] }, ... ], "stagger_seconds": 60,
   "data_port": 4002, "exec_port": 4002 }`.
   (60 s stagger satisfies the 15-s identical-request rule with margin; ports both
   4002 for the single-paper-gateway WSL deployment.)
   Per the no-silent-null-defaults rule: missing required fields must raise, not default.

2. **`fleet_runner.py` supervisor**:
   - Load manifest; for each enabled instance, read its strategy JSON and extract
     `live_config.client_id`. **Validate before launching anything:**
     (a) every config has an explicit `live_config.client_id` (crash if missing);
     (b) all client_ids are unique AND spaced ≥2 apart (each instance consumes
     `cid` and `cid+1` — e.g. 1400/1401, so 1401 in another config is a collision;
     note `IBKRConnectionManager.connect()` silently auto-increments on Error 326,
     which would mask collisions — fail fast at the manifest level instead);
     (c) instance count sanity: 2×N ≤ 32 connections per gateway, 3×N ≤ 100 data lines.
   - Spawn each instance as a subprocess of the existing CLI
     (`conda run -n trader python -m src.live_execution.cli --config <path> ...`),
     sleeping `stagger_seconds` (default 90) between launches to respect historical
     pacing during warm-start backfill.
   - Monitor children: log exits, restart a crashed child with capped exponential
     backoff (LiveTrader already self-restarts on reconnect failure; the runner is the
     outer layer for hard process death). Propagate SIGINT/SIGTERM to all children for
     clean shutdown (they already handle graceful shutdown + cache save).
   - Prefix/route child stdout to per-instance log files (per-cid file logging already
     exists via `_setup_file_logging`; runner just needs to not swallow stderr).

3. **No changes to LiveTrader, DataManager, strategy code, or the training pipeline.**
   Warm-start cache stays shared (already per bar-size, cid-agnostic). Telemetry stays
   per-cid. This preserves parity-gate behavior exactly.

4. **Cloud path (later, out of scope for this ticket):** wrap the same CLI in a
   container image; one service per strategy config; fleet manifest becomes the
   compose/Terraform definition. Only if the fleet grows beyond ~25-30 instances does a
   shared data broadcaster (one subscription per symbol fanned out to consumers) become
   necessary — the advisory hook for this already exists in cli.py:253-260.

## Non-Goals / Risks
- Do not consolidate duplicate symbol subscriptions now: keepUpToDate streams are push,
  not poll; duplicates only cost data lines (3/instance), far under the 100 budget.
- Risk: many instances writing the shared warm-start cache on shutdown — last writer
  wins is acceptable (same symbol, same bars), but keep an eye on it once symbols
  diverge (ZC/ES fleets should use per-symbol seed/cache paths via `live_config`).
