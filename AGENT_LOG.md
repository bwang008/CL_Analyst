# Agent Log

## 2026-05-26 — Headless WSL/Ubuntu Deployment & Systemd Services Activation

### Summary
Implemented a fully autonomous, headless live trading deployment inside WSL 2 (Ubuntu 22.04) with production-grade systemd services (`ibc-gateway.service`, `live-trader.service`), logs management (logrotate), and automatic recovery.

### Implementation
1. **WSL Dependencies & Folder Prep**: Installed system dependencies (`default-jre`, `xvfb`, `socat`, etc.), created dynamic scaffolding directories (`/opt/cl-trader/...`), and chowned to the deploy user.
2. **Conda Python 3.12 Environment**: Recreated the Python execution environment in WSL using Python 3.12 to resolve PyPI package conflicts (such as yanked `pandas-ta` packages requiring Python >=3.12).
3. **Headless IB Gateway via IBC**:
   - Provisioned stable IB Gateway 10.45 and IBC 3.19.0 inside WSL.
   - Resolved path discrepancies using self-referential symlinks (`~/Jts/ibgateway/10.45 -> ~/Jts/ibgateway`) and folder mappings.
   - Configured headless GUI logins using the modern `xvfb-run` wrapper.
4. **Self-Healing Systemd Dependency Stack**:
   - `ibc-gateway.service`: Starts the automated headless gateway.
   - `live-trader.service`: Starts the Python engine. Formulated a pre-flight socket validation loop (`ExecStartPre`) that holds the Python startup sequence until port 4002 successfully responds. Added robust crash-recovery and shutdown timeouts.
5. **Log Management**: Deployed logrotate configurations to manage storage footprints via daily compression and a 14-day retention limit.
6. **Troubleshooting & Porting Optimization**:
   - Identified and resolved a network porting trap where a local `.env` configuration overrode the loopback IP with the Windows host hypervisor IP (`172.28.16.1`). Resolved this by explicitly defining `--host 127.0.0.1` in the systemd script and sanitizing the local `.env`.
   - Scripted a completely environment-agnostic setup automation utility `deploy/setup_ubuntu.sh` equipped with user path interpolation and TightVNC integration for cloud VPS environments.

### Validation Results
- Logged on successfully to the paper trading server.
- The Python live trader dynamically loaded the ensemble strategy (`HourSet_08_Ensemble_03_05242026`), mapped the correct models (201 features each), and qualified the NYMEX Crude Oil continuous contract (`CLN6`).
- Heartbeats, telemetry logs (`/opt/cl-trader/data/data/live_telemetry_cid1010.db`), and Telegram alert mechanisms verified fully functional in the background.

## 2026-05-25 — Global Execution Guard: Trade Pattern Analysis + Implementation

### Summary
Analyzed losing trade patterns for `HourSet_08_Ensemble_03_05242026`, identified structurally toxic entry windows, and implemented a Global Execution Guard system with config inheritance.

### Trade Pattern Analysis
- Ran backtest (513 trades, $91,236 PnL baseline) and analyzed losing patterns by hour, day-of-week, and holiday proximity.
- **9:00 AM EST (8:00 bar)**: -$14,898 cumulative PnL, 50% win rate. NYMEX pit open whipsaw.
- **11:00 AM EST (11:00 bar)**: -$6,494 net PnL despite 68.9% win rate. EIA inventory whipsaw (primarily Wednesdays).
- **Tuesday toxicity**: Only day with negative cumulative PnL (-$2,924). Driven by pre-API positioning.
- **Long weekend transitions**: AFTER_LONG_WEEKEND lost -$10,493, BEFORE_LONG_WEEKEND lost -$3,703.

### Implementation
1. **`configs/global_risk_filters.json`**: Global house rules (blocked hours, holiday blocking).
2. **`src/live_execution/config_loader.py`**: Centralized config loader with inheritance. Merges global filters into every strategy unless `override_global_filters: true`.
3. **`src/live_execution/execution_guard.py`**: Guard class with `is_entry_allowed(timestamp)`. Blocks entry hours and long-weekend adjacent days. Edge-triggered toggle logging (`[GUARD ACTIVATED]` / `[GUARD DEACTIVATED]`).
4. **`agent/backtest_engine.py`**: Integrated guard into `from_config()` and both single/concurrent strategy runners.
5. **`src/live_execution/strategies/configurable_strategy.py`**: Swapped raw `json.load()` to `load_strategy_config()` for live trader config inheritance. Guard check in `evaluate()` returns HOLD with `skip_reason="EXECUTION_GUARD"`.
6. **`src/live_execution/live_trader.py`**: Added `EXECUTION_GUARD` branch to HOLD handler for clear live terminal logging.
7. **`tests/test_execution_guard.py`**: 10 unit tests (hours, holidays, overrides, timezones, edge-triggered logging).

### Validation Results
- **56/56 tests pass** (10 guard + 46 engine).
- **Guarded backtest**: 486 trades, $97,265 PnL (+$6,029), PF 1.42 (+0.06), Max DD -$18,582 (+$3,471).
- **Holdout**: 60 trades, $10,380 PnL, PF 1.56, Max DD -$3,112.

### Documentation
- Updated `README.md` with Global Risk Filters section, config reference, design rules, and project structure.
- Created `AGENT_LOG.md` (this file).

### Known Limitation
- **11:00 AM block is every day**, not Wednesday-only. The EIA-driven toxicity is primarily Wednesdays; blocking other days may remove some profitable trades. A follow-up task to implement day-of-week-specific hour blocking is planned.

## 2026-06-19 — Cloud Pipeline Dependency Resiliency & Post-Optimizer Fixes

### Summary
Diagnosed and resolved a critical pipeline outage in /run-cloud-batch where the optuna-post-optimizer phase silently failed due to Python package version drift, resulting in empty ensemble output.

### Problems Encountered
1. **Pandas-TA 0.4.71b0 Python Version Conflict:** The pandas-ta library requires Python 3.12+, causing installation failures on the default GCP Ubuntu 22.04 LTS images (Python 3.10).
2. **Pandas 3.0 Breaking Changes:** Resolving the Python version issue triggered an automatic upgrade to pandas-3.0.3 (because pandas-ta==0.4.71b0 forces pandas>=2.3.2). The post-optimizer script (gent/strategy_optimizer.py) crashed with a ValueError because Pandas 3.0 removed the 'M' offset alias (monthly resampling).
3. **Silent Orchestrator Failure:** Because the strategy_optimizer.py script crashed on the VM, it saved JSON results with "status": "FAILED". The orchestrator (gcp_deploy_optimizer.ps1) downloaded these results without raising a fatal error. The subsequent local ensemble script (sweep_ensembles.py) then correctly skipped execution due to "0 trades", masking the upstream crash.

### Implementation & Fixes
1. **gcp/vm_startup.sh:** Updated the VM provisioning script to explicitly add the ppa:deadsnakes/ppa repository and install python3.12 and python3.12-venv natively.
2. **gent/strategy_optimizer.py & gent/alpha_evaluator.py:** Migrated all .resample('M') calls to .resample('ME') to comply with Pandas 2.2+ and Pandas 3.0+ deprecation standards.
3. **Validation:** Successfully re-ran the optuna-post-optimizer VM for atch_20260618_1721 with the updated code. The pipeline completed without outages and successfully generated all atch_summary_optimized_ensembles_*.md reports.

### Gotchas for Future Agents
- **Dependency Drift:** Be aware that the pipeline installs pandas-ta==0.4.71b0, which actively forces pandas>=2.3.2 (Pandas 3.0+). You cannot pin an older Pandas version (like 2.2.2) globally in the m_startup.sh script or the build will fail with a resolution error.
- **Zombie VMs:** If the un_sweep_batch.ps1 orchestrator is forcefully cancelled by the user during VM provisioning, GCP quota may become exhausted due to hanging optuna-sweep-* instances. Run gcloud compute instances list and clean up zombies before initiating a fresh batch.
- **Quota Exhaustion:** The post-optimizer dynamically spins up large machines (e.g., 
2-standard-32). If a zone (e.g., us-central1-a) returns ZONE_RESOURCE_POOL_EXHAUSTED, the orchestrator will fail. The orchestrator may need its -Zone argument adjusted (e.g., us-west1-b) if the primary zone is out of resources.
