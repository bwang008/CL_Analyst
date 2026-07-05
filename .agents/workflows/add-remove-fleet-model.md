---
name: add-remove-fleet-model
description: Add a trained strategy config to (or remove one from) the fleet manifest, validate every reference before launch, and verify via staged dry-run before going live
---

# /add-remove-fleet-model — Manage models in the live fleet

The fleet runner (`src/live_execution/fleet_runner.py`) launches one `live_trader`
process per strategy config listed in **`configs/fleet/fleet_manifest.json`**, with
staggered starts (IBKR pacing), per-child crash restarts, and SIGTERM fan-out.
This workflow adds/removes a model safely: validate → dry-run → live.

> **Env:** every command runs as `conda run -n trader python ...` from the repo root.

## Manifest format (`configs/fleet/fleet_manifest.json`)
```json
{
  "instances": [
    { "config": "configs/strategies/<MODEL>.json", "enabled": true, "extra_args": [] },
    { "config": "configs/strategies/<NEW>.json",   "enabled": true, "extra_args": ["--dry-run"] }
  ],
  "stagger_seconds": 60,
  "data_port": 4002,
  "exec_port": 4002
}
```
- `extra_args` are appended to that instance's CLI invocation. **New models enter with
  `["--dry-run"]`** (connects, subscribes, runs inference — places NO orders) and only
  lose the flag after a clean observation window.
- `enabled: false` keeps an entry parked without deleting it (preferred over removal
  for temporary stand-downs).
- All fields are required — the runner raises on missing fields (no silent defaults).

## ADD a model
1. **Config prerequisites** (the strategy JSON must carry):
   - `execution_symbol` matching the model's symbol (micros like MES/MCL are valid —
     brain resolves to the parent; model `symbol` fields validate against the brain).
   - `live_config.client_id` — UNIQUE across the manifest and **spaced ≥ 2** from every
     other (each instance consumes cid AND cid+1). Also avoid cids of any manually
     started instance still connected to the gateway.
   - `live_config.enable_5m_stream: false` for any non-CL hourly model (no 5m seeds
     exist for new symbols by design — all data is hourly).
2. **Per-symbol data prerequisites** (for a symbol's FIRST fleet model):
   - 1h seed `CL_DATA_ROOT/data/processed/{SYM}_raw_1h.parquet` (brain symbol, e.g. ES
     for an MES config). Copy from `{SYM}_raw.parquet` if absent (it is hourly).
   - Macro CSVs `data/raw/macro/fred_macro_data_{sym}.csv` + `cftc_cot_{sym}.csv`
     (see /build-symbol-pipeline Phase 2).
   - Model pkls + predictions CSV at the paths the config references.
3. **Add the manifest entry** with `"extra_args": ["--dry-run"]`.
4. **VALIDATION GATE (blocking — run all three):**
   ```bash
   # (a) config resolves (instrument + model-tag validation)
   conda run -n trader python -c "import json; from src.live_execution.instrument_context import resolve_instrument_context as r; ctx=r(json.load(open('configs/strategies/<NEW>.json'))); print('OK:', ctx.execution_symbol, '/', ctx.brain_symbol)"
   # (b) whole-manifest validation (client_id uniqueness/spacing, capacity, file existence)
   conda run -n trader python -c "from src.live_execution.fleet_runner import FleetRunner; f=FleetRunner(manifest_path='configs/fleet/fleet_manifest.json'); f.load_manifest(); f.validate(); print('MANIFEST OK:', len([i for i in f.manifest['instances'] if i['enabled']]), 'enabled instances')"
   # (c) referenced artifacts exist (model_path / predictions_path / seeds) — resolver
   #     covers config-internal checks; spot-check the per-symbol seed:
   #     Test-Path $env:CL_DATA_ROOT\data\processed\{SYM}_raw_1h.parquet
   ```
   Any raise = fix before proceeding. Do NOT launch a fleet whose validation fails.
5. **Pre-launch check:** no manually started live_trader may hold this config's cids
   (or the cids of any OTHER manifest entry!). A collision triggers IBKR error 326 and
   client-id auto-increment — for a LIVE config that means a DUPLICATE trader. Check:
   `Get-CimInstance Win32_Process -Filter "Name='python.exe'" | ? {$_.CommandLine -match 'live_execution'}`
   and stop stale instances (`Stop-Process -Id <pid>`).
6. **Dry-run stage — launch the fleet and observe:**
   ```bash
   conda run -n trader python -m src.live_execution.fleet_runner --manifest configs/fleet/fleet_manifest.json
   ```
   Per-instance evidence lives in `reports/livetrader_<client_id>.log`. Success for the
   new model: instrument-resolution banner (right symbol/exchange/tick), symbol-named
   DATA PATHS lines, front-month contract of the RIGHT symbol, subscriptions, heartbeat.
   For a non-CL model also grep its log for CL leakage: `Select-String -Path
   reports\livetrader_<cid>.log -Pattern "Front-month contract: CL"` → must be empty.
7. **Go live:** after a clean observation window (ideally including live bars during
   market hours), remove `"--dry-run"` from `extra_args`, restart the fleet.

## REMOVE a model
1. Prefer `"enabled": false` (keeps history/config wiring intact).
2. Stop the fleet (Ctrl+C / SIGTERM — the runner fans out; children cancel
   subscriptions and save caches) or stop just the child process.
3. **If the model holds an open position**, closing it is a MANUAL decision — the
   fleet stop does not flatten positions (GTC brackets remain at IBKR). Check the
   instance's telemetry DB / TWS before and after.
4. Re-run validation gate (b) and relaunch.

## Failure signatures
| Symptom | Meaning |
|---|---|
| `ValueError ... execution_symbol` / `does not match model symbol` | Config fails T1 validation — wrong/missing symbol fields |
| `FileNotFoundError ... _raw_1h.parquet` | Missing per-symbol 1h seed (step 2) |
| `FileNotFoundError ... fred_macro_data_<sym>.csv` / COT | Missing macro CSVs (step 2) |
| IBKR error 326 in a child log | client_id collision — stale manual instance or bad spacing |
| Runner raises before any launch | Manifest validation failed — nothing was started (by design) |
| Child restart loop in runner output | Child crashes post-launch — read its `livetrader_<cid>.log` |

## Key files
| File | Purpose |
|------|---------|
| `configs/fleet/fleet_manifest.json` | The fleet definition |
| `src/live_execution/fleet_runner.py` | Supervisor (validate/launch/stagger/restart) |
| `src/live_execution/instrument_context.py` | Config resolver (validation gate a) |
| `reports/livetrader_<cid>.log` | Per-instance log |
| `deploy/systemd/fleet-runner.service` | Boot-time fleet on WSL/cloud |
