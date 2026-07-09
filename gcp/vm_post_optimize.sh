#!/bin/bash
# =============================================================================
# Post-Optimizer VM Run Script
#
# Runs batch_post_optimizer.py on a cloud VM with full parallelism.
# Downloads all experiment artifacts from GCS, runs optimization, and
# uploads results back to GCS.
#
# Usage (called by gcp_deploy_optimizer.ps1):
#   bash gcp/vm_post_optimize.sh --batch-id=batch_20260513_1941 \
#       --n-trials=1000 --holdout-months=4 --workers=24 [--shutdown]
#
# Arguments:
#   --batch-id=<id>        Batch ID to optimize
#   --n-trials=<n>         Optuna trials per optimization (default: 1000)
#   --holdout-months=<n>   Holdout months for validation (default: 4)
#   --workers=<n>          Parallel workers (default: 24)
#   --shutdown             Auto-shutdown VM after completion
# =============================================================================

set -e
# pipefail (ticket optimizer-vm-deploy-whitelist_07052026_1820): every python/gsutil
# step is piped through `tee`, whose exit 0 masked the real failure under set -e —
# the ModuleNotFoundError crashes limped on to a misleading "0 pairs" FATAL.
# Intentional soft-failures keep their `|| true` / `|| {... }` guards.
set -o pipefail

export PYTHONIOENCODING=utf8

cleanup() {
    local exit_code=$?
    if [ $exit_code -ne 0 ]; then
        echo "============================================================" | tee -a "$LOG"
        echo " TRAP TRIGGERED: Script exited with code $exit_code" | tee -a "$LOG"
        echo "============================================================" | tee -a "$LOG"

        # --- Root-cause extraction --------------------------------------
        # The fatal error is often hundreds of lines above the tail (e.g. an
        # early ModuleNotFoundError that only surfaces at the end as
        # "0 pairs"). Pull the LAST Python traceback plus FATAL/ERROR lines
        # so the alert carries the reason, not just the symptom.
        ERR_SUMMARY=$(python3 - "$LOG" <<'PYEOF'
import re, sys

try:
    lines = open(sys.argv[1], errors="replace").read().splitlines()
except OSError as e:
    print(f"(could not read log: {e})")
    raise SystemExit

picked = []
tb_start = None
for i in range(len(lines) - 1, -1, -1):
    if lines[i].startswith("Traceback (most recent call last)"):
        tb_start = i
        break
if tb_start is not None:
    picked += ["-- last traceback --"] + lines[tb_start:tb_start + 15]

pat = re.compile(r"FATAL|MANIFEST ERROR|ModuleNotFoundError|ERROR|Exception|No such file", re.I)
skip = re.compile(r"^\s*(File |Traceback)")
errs = []
seen = set()
for l in lines:
    if pat.search(l) and not skip.match(l):
        s = l.strip()[:200]
        if s not in seen:
            seen.add(s)
            errs.append(s)
if errs:
    head = errs[:3]
    tail = [e for e in errs[-5:] if e not in head]
    mid = ["..."] if len(errs) > 8 else []
    picked += ["-- error lines (first->last) --"] + head + mid + tail

out = "\n".join(picked) if picked else "(no error pattern found; see full log)"
print(out[:2500])
PYEOF
) || ERR_SUMMARY="(root-cause extraction failed; see full log)"

        # --- Persist evidence BEFORE alerting/self-deleting --------------
        GCS_LOG_PATH="(unknown)"
        if [ -n "$GCS_OPT_PREFIX" ] && [ -n "$LOG" ]; then
            GCS_LOG_PATH="$BUCKET/$GCS_OPT_PREFIX/logs/$(basename "$LOG")"
            gsutil cp "$LOG" "$BUCKET/$GCS_OPT_PREFIX/logs/" 2>/dev/null || GCS_LOG_PATH="(log upload FAILED)"
            {
                echo "EXIT CODE: $exit_code"
                echo "BATCH: $BATCH_ID"
                echo "FULL LOG: $GCS_LOG_PATH"
                echo ""
                echo "ROOT-CAUSE SUMMARY:"
                echo "$ERR_SUMMARY"
            } > /tmp/CRASH_REPORT.txt
            gsutil cp /tmp/CRASH_REPORT.txt "$BUCKET/$GCS_OPT_PREFIX/CRASH_REPORT.txt" 2>/dev/null || true
        fi

        # --- Telegram alert with the REASON and the persisted log path ---
        python3 -c "
import html, sys, urllib.request, json
try:
    with open('/home/$(whoami)/project/.env') as f:
        env = dict(line.strip().split('=', 1) for line in f if '=' in line and not line.startswith('#'))
    token = env.get('TELEGRAM_BOT_TOKEN')
    chat_id = env.get('TELEGRAM_CHAT_ID')
    if token and chat_id:
        exit_code, summary, batch, gcs_log = sys.argv[1:5]
        msg = (f'🚨 <b>[VM CRASH] Post-Optimizer</b>\nBatch: <code>{html.escape(batch)}</code>\n'
               f'Exit Code: {html.escape(exit_code)}\n\n'
               f'<b>Root cause:</b>\n<pre>{html.escape(summary[:2800])}</pre>\n\n'
               f'Full log: <code>{html.escape(gcs_log)}</code>')
        req = urllib.request.Request(f'https://api.telegram.org/bot{token}/sendMessage',
            data=json.dumps({'chat_id': chat_id, 'text': msg[:4000], 'parse_mode': 'HTML'}).encode('utf-8'),
            headers={'Content-Type': 'application/json'})
        urllib.request.urlopen(req, timeout=10)
except Exception as e:
    print('Failed to send telegram:', e)
" "$exit_code" "$ERR_SUMMARY" "$BATCH_ID" "$GCS_LOG_PATH" || true
    fi

    # Upload logs before dying
    if [ -n "$GCS_OPT_PREFIX" ] && [ -n "$LOG" ]; then
        echo "Uploading final logs to gs://$BUCKET/$GCS_OPT_PREFIX/logs/"
        gsutil cp "$LOG" "$BUCKET/$GCS_OPT_PREFIX/logs/" 2>/dev/null || true
    fi

    # Delete VM if requested
    if [ "$SHUTDOWN" = true ]; then
        echo "Self-deleting optimizer VM in 15 seconds..." | tee -a "$LOG"
        sleep 15
        VM_NAME=$(curl -s -H "Metadata-Flavor: Google" http://metadata.google.internal/computeMetadata/v1/instance/name || echo "")
        VM_ZONE=$(curl -s -H "Metadata-Flavor: Google" http://metadata.google.internal/computeMetadata/v1/instance/zone | awk -F/ '{print $NF}' || echo "")
        if [ -n "$VM_NAME" ] && [ -n "$VM_ZONE" ]; then
            gcloud compute instances delete "$VM_NAME" --zone="$VM_ZONE" --quiet 2>/dev/null || sudo shutdown -h now
        else
            sudo shutdown -h now
        fi
    fi
}
trap cleanup EXIT

# Wait for startup script to finish if it hasn't already (guards against early SSH execution)
if [ ! -f /tmp/startup_done ]; then
    echo "Waiting for startup script to complete..." | tee -a "$LOG"
    for i in {1..60}; do
        if [ -f /tmp/startup_done ]; then
            break
        fi
        sleep 10
    done
    if [ ! -f /tmp/startup_done ]; then
        echo "FATAL: Startup script did not complete in time." | tee -a "$LOG"
        exit 1
    fi
fi

# Activate environment (guard against non-interactive shell issues with set -e)
set +e
source /opt/optuna-env/bin/activate
set -e

PROJECT_DIR="/home/$(whoami)/project"
cd "$PROJECT_DIR"

# Defaults
BATCH_ID=""
N_TRIALS=500
# HOLDOUT_MONTHS is NOT defaulted here — it is read authoritatively from the manifest
# (post_optimizer_holdout_months) below. The manifest is the single source of truth.
HOLDOUT_MONTHS=""
WORKERS=0
SHUTDOWN=false
# Sortino dropped 2026-07-04 (ticket drop-sortino-objective_07042026_2301):
# default is sharpe-only (defense-in-depth for manual VM invocations; the managed
# path always passes --objective= explicitly). Pass --objective=both to roll back.
OBJECTIVE="sharpe"
SWEEP_MODE="backtest"
OPT_MODE="individual"
BUCKET="gs://cltrainer-optuna-results"
LOG="post_optimize_$(date +%Y%m%d_%H%M%S).log"

# Parse args
for arg in "$@"; do
    case "$arg" in
        --batch-id=*) BATCH_ID="${arg#*=}" ;;
        --n-trials=*) N_TRIALS="${arg#*=}" ;;
        # --holdout-months is intentionally IGNORED if passed: the manifest's
        # post_optimizer_holdout_months is authoritative (set after manifest parse below).
        --holdout-months=*) : ;;
        --workers=*) WORKERS="${arg#*=}" ;;
        --objective=*) OBJECTIVE="${arg#*=}" ;;
        --sweep-mode=*) SWEEP_MODE="${arg#*=}" ;;
        --opt-mode=*) OPT_MODE="${arg#*=}" ;;
        --shutdown) SHUTDOWN=true ;;
    esac
done

if [ -z "$BATCH_ID" ]; then
    echo "ERROR: --batch-id is required" | tee -a "$LOG"
    exit 1
fi

GCS_OPT_PREFIX="batch_optimizer/$BATCH_ID"
START_TIME=$(date +%s)

echo "" | tee -a "$LOG"
echo "============================================================" | tee -a "$LOG"
echo " POST-OPTIMIZER VM RUN" | tee -a "$LOG"
echo "============================================================" | tee -a "$LOG"
echo "  Batch ID:      $BATCH_ID" | tee -a "$LOG"
echo "  N Trials:      $N_TRIALS" | tee -a "$LOG"
echo "  Holdout:       (read from manifest below)" | tee -a "$LOG"
echo "  Workers:       $WORKERS" | tee -a "$LOG"
echo "  Objective:     $OBJECTIVE" | tee -a "$LOG"
echo "  Sweep Mode:    $SWEEP_MODE" | tee -a "$LOG"
echo "  Opt Mode:      $OPT_MODE" | tee -a "$LOG"
echo "  Bucket:        $BUCKET" | tee -a "$LOG"
echo "  CPUs:          $(nproc)" | tee -a "$LOG"
echo "============================================================" | tee -a "$LOG"

# --- [1/5] Download batch metadata ---
echo "" | tee -a "$LOG"
echo "[1/5] Downloading batch metadata from GCS..." | tee -a "$LOG"
BATCH_DIR="reports/batch_runs/$BATCH_ID"
mkdir -p "$BATCH_DIR"
gsutil cp "$BUCKET/$GCS_OPT_PREFIX/batch_progress.json" "$BATCH_DIR/batch_progress.json" 2>&1 | tee -a "$LOG" || { echo "  FATAL: batch_progress.json download failed!" | tee -a "$LOG"; exit 1; }
gsutil cp "$BUCKET/$GCS_OPT_PREFIX/manifest.json" "$BATCH_DIR/manifest.json" 2>&1 | tee -a "$LOG" || { echo "  FATAL: manifest.json download failed!" | tee -a "$LOG"; exit 1; }

# Extract OHLCV path + strategy/exec/slippage/holdout from manifest.
# The MANIFEST IS THE SINGLE SOURCE OF TRUTH: every operational parameter is REQUIRED
# here and parsing FAILS LOUDLY if any is missing — no silent default may stand in.
# Supports v2 manifest (baseline.* structure) and legacy (defaults.* structure).
_PARSED=$(python3 - "$BATCH_DIR/manifest.json" <<'PYEOF' 2>&1
import json, os, sys
m = json.load(open(sys.argv[1]))

def fail(msg):
    sys.stderr.write("MANIFEST ERROR: " + msg + "\n")
    sys.exit(1)

b = m.get("baseline")
if b:  # v2 manifest format
    sym = (b.get("symbol") or "").strip()
    dv = (b.get("data_workflow", {}) or {}).get("dataset_version", "") or ""
    # Mirror sweep deploy/orchestrator: avoid redundant symbol prefix
    if dv.upper().startswith(sym.upper()):
        dataset_name = f"{dv}.parquet"
    else:
        dataset_name = f"{sym}_{dv}.parquet"
    ohlcv = f"gs://cltrainer-optuna-results/data/{dataset_name}"
    ew = b.get("execution_workflow") or {}
    strat_path = ew.get("strategy_config_path")
    if not strat_path:
        fail("baseline.execution_workflow.strategy_config_path is required (no default).")
    strat = os.path.basename(strat_path)
    exec_data = ew.get("execution_data_path") or ""
    if ew.get("slippage_per_side") is None:
        fail("baseline.execution_workflow.slippage_per_side is required (no default).")
    slip = ew["slippage_per_side"]
    tw = b.get("training_workflow") or {}
    opt = tw.get("optuna") or {}
    if opt.get("post_optimizer_holdout_months") is None:
        fail("baseline.training_workflow.optuna.post_optimizer_holdout_months is required (no default).")
    holdout = opt["post_optimizer_holdout_months"]
    # Global RNG seed — REQUIRED (no default). Threaded to strategy_optimizer via
    # batch_post_optimizer.py so the post-optimization stage is reproducible.
    if tw.get("random_seed") is None:
        fail("baseline.training_workflow.random_seed is required (no default).")
    seed = tw["random_seed"]
else:  # legacy manifest format
    d = m.get("defaults") or {}
    ohlcv = d.get("gcs_data_path") or ""
    strat = d.get("strategy_config") or ""
    if not strat:
        fail("defaults.strategy_config is required (no default).")
    exec_data = d.get("exec_data") or ""
    if d.get("slippage_per_side") is None:
        fail("defaults.slippage_per_side is required (no default).")
    slip = d["slippage_per_side"]
    if d.get("post_optimizer_holdout_months") is None:
        fail("defaults.post_optimizer_holdout_months is required (no default).")
    holdout = d["post_optimizer_holdout_months"]
    if d.get("random_seed") is None:
        fail("defaults.random_seed is required (no default).")
    seed = d["random_seed"]

if not ohlcv:
    fail("could not resolve OHLCV gcs data path from manifest.")
if float(slip) > 0.5:
    fail(f"slippage_per_side={slip} is implausibly large (>$0.50/side). This is the -$2.5M bug class; aborting.")

print(ohlcv)
print(strat)
print(exec_data)
print(slip)
print(holdout)
print(seed)
PYEOF
)
if [ $? -ne 0 ]; then
    echo "  FATAL: manifest parsing failed — a required parameter is missing or invalid." | tee -a "$LOG"
    echo "$_PARSED" | tee -a "$LOG"
    exit 1
fi
mapfile -t _MF <<< "$_PARSED"
OHLCV_GCS="${_MF[0]}"
STRATEGY_CONFIG="${_MF[1]}"
EXEC_DATA_GCS="${_MF[2]}"
SLIPPAGE_PER_SIDE="${_MF[3]}"
HOLDOUT_MONTHS="${_MF[4]}"   # authoritative: from manifest post_optimizer_holdout_months
RANDOM_SEED="${_MF[5]}"      # authoritative: from manifest training_workflow.random_seed
OHLCV_BASENAME=$(basename "$OHLCV_GCS")
echo "  OHLCV GCS path: $OHLCV_GCS" | tee -a "$LOG"
echo "  Strategy config: $STRATEGY_CONFIG" | tee -a "$LOG"
echo "  Slippage/side:  $SLIPPAGE_PER_SIDE  (absolute price units, from manifest)" | tee -a "$LOG"
echo "  Holdout:        $HOLDOUT_MONTHS months (from manifest)" | tee -a "$LOG"
echo "  Random seed:    $RANDOM_SEED (from manifest)" | tee -a "$LOG"

if [ -z "$HOLDOUT_MONTHS" ]; then
    echo "  FATAL: HOLDOUT_MONTHS empty after manifest parse — refusing to run." | tee -a "$LOG"
    exit 1
fi

if [ -z "$RANDOM_SEED" ]; then
    echo "  FATAL: RANDOM_SEED empty after manifest parse — refusing to run (reproducibility required)." | tee -a "$LOG"
    exit 1
fi

# --- [2/5] Download OHLCV data ---
echo "" | tee -a "$LOG"
echo "[2/5] Downloading OHLCV data from GCS..." | tee -a "$LOG"
mkdir -p data/processed
gsutil cp "$OHLCV_GCS" "data/processed/$OHLCV_BASENAME" 2>&1 | tee -a "$LOG"
echo "  OHLCV data ready ($(du -h data/processed/$OHLCV_BASENAME | cut -f1))" | tee -a "$LOG"

EXEC_DATA_PATH=""
if [ -n "$EXEC_DATA_GCS" ] && [[ "$EXEC_DATA_GCS" == gs://* ]]; then
    EXEC_DATA_BASENAME=$(basename "$EXEC_DATA_GCS")
    gsutil cp "$EXEC_DATA_GCS" "data/processed/$EXEC_DATA_BASENAME" 2>&1 | tee -a "$LOG"
    EXEC_DATA_PATH="data/processed/$EXEC_DATA_BASENAME"
    echo "  EXEC_DATA ready ($(du -h $EXEC_DATA_PATH | cut -f1))" | tee -a "$LOG"
elif [ -n "$EXEC_DATA_GCS" ]; then
    EXEC_DATA_PATH="$EXEC_DATA_GCS"
fi

# Fail-fast guard: the strategy trains/signals on ratio-adjusted data but MUST execute + track
# PnL on the raw UNADJUSTED series (execution_data_path). With no exec data, the optimizer and the
# ensemble backtest silently fall back to DIFFERENT adjusted sources -> summary != backtest report,
# and all PnL is computed on adjusted prices. This class of silent error burned batch_20260630_1037.
if [ -z "$EXEC_DATA_PATH" ]; then
    echo "  FATAL: execution_data_path is empty. This pipeline executes PnL on raw unadjusted data;" | tee -a "$LOG"
    echo "         running without it produces wrong AND internally-inconsistent PnL. Set" | tee -a "$LOG"
    echo "         baseline.execution_workflow.execution_data_path in the manifest and re-run." | tee -a "$LOG"
    exit 1
fi

OPT_ARGS=""
if [ -n "$EXEC_DATA_PATH" ]; then
    OPT_ARGS="--exec-data $EXEC_DATA_PATH"
fi
if (( $(echo "$SLIPPAGE_PER_SIDE > 0" | bc -l) )); then
    OPT_ARGS="$OPT_ARGS --slippage-per-side $SLIPPAGE_PER_SIDE"
fi
# Seed threaded into batch_post_optimizer.py -> strategy_optimizer (Task 1 consumer).
OPT_ARGS="$OPT_ARGS --random-seed $RANDOM_SEED"

# --- [3/5] Download all experiment artifacts from GCS ---
echo "" | tee -a "$LOG"
echo "[3/5] Downloading experiment artifacts from GCS..." | tee -a "$LOG"

# Parse batch_progress.json and download each experiment's artifacts
python3 -c "
import json, os, subprocess, sys

bp = json.load(open('$BATCH_DIR/batch_progress.json', encoding='utf-8-sig'))
experiments = bp.get('experiments', [])
project_dir = '$PROJECT_DIR'
bucket = '$BUCKET'

for exp in experiments:
    if exp.get('status') != 'COMPLETED':
        continue
    prefix = exp.get('gcs_prefix', '')
    if not prefix:
        continue
    
    # Create local directory matching batch_progress layout
    local_dir = os.path.join(project_dir, 'reports', prefix)
    os.makedirs(os.path.join(local_dir, 'registry'), exist_ok=True)
    
    # Download production artifacts zip
    gcs_zip = f'{bucket}/{prefix}/production/'
    print(f'  Downloading artifacts for {prefix}...')
    subprocess.run(['gsutil', '-m', 'cp', '-r', f'{gcs_zip}*.zip', local_dir], 
                    capture_output=True)
    
    # Unzip into registry/
    import glob
    for zf in glob.glob(os.path.join(local_dir, '*.zip')):
        subprocess.run(['unzip', '-o', '-q', zf, '-d', os.path.join(local_dir, 'registry')],
                        capture_output=True)
        print(f'    Unpacked: {os.path.basename(zf)}')
    
    # Detect artifact layout: v2 uses production_output/, legacy uses canary_output/
    prod_dir = os.path.join(local_dir, 'registry', 'production_output')
    canary_dir = os.path.join(local_dir, 'registry', 'canary_output')
    if os.path.isdir(prod_dir) and not os.path.isdir(canary_dir):
        print(f'    Layout: v2 (production_output/)')
    elif os.path.isdir(canary_dir):
        print(f'    Layout: legacy (canary_output/)')
    else:
        # Fallback: create canary_output for legacy compatibility
        os.makedirs(canary_dir, exist_ok=True)
        print(f'    Layout: unknown (created canary_output/)')
    
    # Also download pipeline_summary.json
    subprocess.run(['gsutil', 'cp', f'{bucket}/{prefix}/pipeline_summary.json', 
                     os.path.join(local_dir, 'pipeline_summary.json')], capture_output=True)
    
    # Update local_dir in batch_progress to Linux path
    exp['local_dir'] = local_dir

# Rewrite batch_progress.json with Linux paths
with open('$BATCH_DIR/batch_progress.json', 'w') as f:
    json.dump(bp, f, indent=2)
print(f'  Updated batch_progress.json with {len(experiments)} experiments (Linux paths)')
" 2>&1 | tee -a "$LOG"

# Verify artifacts
ARTIFACT_COUNT=$(find reports/ -name "*.csv" \( -path "*/canary_output/*" -o -path "*/production_output/*" \) | wc -l)
echo "  Found $ARTIFACT_COUNT prediction CSVs" | tee -a "$LOG"

CONFIG_COUNT=$(find reports/ -name "*.json" \( -path "*/canary_output/*" -o -path "*/production_output/*" \) -not -name "pipeline_*" | wc -l)
echo "  Found $CONFIG_COUNT ensemble configs" | tee -a "$LOG"

if [ "$ARTIFACT_COUNT" -eq 0 ]; then
    echo "  ERROR: No prediction CSVs found! Check GCS artifacts." | tee -a "$LOG"
    exit 1
fi

if [ "$OPT_MODE" = "ensemble" ]; then
    # --- [3b/5] Sweep Ensembles ---
    echo "" | tee -a "$LOG"
    echo "[3b/5] Sweeping Ensembles (Baseline Config)..." | tee -a "$LOG"
    SWEEP_ARGS="--base-config configs/strategies/$STRATEGY_CONFIG \
        --data data/processed/$OHLCV_BASENAME \
        --long-dir reports/ \
        --short-dir reports/ \
        --long-prefix _long_ \
        --short-prefix _short_ \
        --output-md $BATCH_DIR/batch_ensemble_pre_opt.md \
        --mode $SWEEP_MODE \
        --holdout-months $HOLDOUT_MONTHS"
    echo "  Sweep command: python agent/sweep_ensembles.py $SWEEP_ARGS" | tee -a "$LOG"
    python agent/sweep_ensembles.py \
        --base-config configs/strategies/$STRATEGY_CONFIG \
        --data data/processed/$OHLCV_BASENAME \
        --long-dir reports/ \
        --short-dir reports/ \
        --long-prefix "_long_" \
        --short-prefix "_short_" \
        --output-md "$BATCH_DIR/batch_ensemble_pre_opt.md" \
        --mode "$SWEEP_MODE" \
        --holdout-months "$HOLDOUT_MONTHS" \
        2>&1 | tee -a "$LOG"

    echo "  Selecting Top 8 Ensembles..." | tee -a "$LOG"
    python agent/select_top_ensembles.py \
        --md-report "$BATCH_DIR/batch_ensemble_pre_opt.md" \
        --output-json "$BATCH_DIR/top_8_ensembles.json" \
        --top-n 8 \
        2>&1 | tee -a "$LOG"

    PAIR_COUNT=$(python3 -c "import json, os; print(len(json.load(open('$BATCH_DIR/top_8_ensembles.json')))) if os.path.exists('$BATCH_DIR/top_8_ensembles.json') else print(0)")
    if [ "$PAIR_COUNT" -eq 0 ]; then
        echo "  FATAL ERROR: select_top_ensembles.py produced 0 pairs! Aborting to prevent silent failure." | tee -a "$LOG"
        exit 1
    fi

    # --- [4/5] Run batch_post_optimizer (ensemble mode) ---
    echo "" | tee -a "$LOG"
    echo "[4/5] Running batch post-optimizer on Top 8 (ensemble mode)..." | tee -a "$LOG"
    echo "  Command: python agent/batch_post_optimizer.py --batch-dir $BATCH_DIR --target-pairs-json $BATCH_DIR/top_8_ensembles.json --n-trials $N_TRIALS --holdout-months $HOLDOUT_MONTHS --workers $WORKERS --objective $OBJECTIVE --no-filter $OPT_ARGS" | tee -a "$LOG"

    python agent/batch_post_optimizer.py \
        --batch-dir "$BATCH_DIR" \
        --target-pairs-json "$BATCH_DIR/top_8_ensembles.json" \
        --n-trials "$N_TRIALS" \
        --holdout-months "$HOLDOUT_MONTHS" \
        --workers "$WORKERS" \
        --objective "$OBJECTIVE" \
        --no-filter \
        $OPT_ARGS \
        2>&1 | tee -a "$LOG"

    # Generate ensemble backtest artifacts (markdown verification reports).
    # Mirrors the individual-mode branch; inputs (optimization_results_ensembles_*.json,
    # manifest.json, registry/{production_output|canary_output}/) are all produced above.
    echo "" | tee -a "$LOG"
    echo "  Generating ensemble backtest artifacts..." | tee -a "$LOG"
    ENS_ART_ARGS="--batch-dir $BATCH_DIR --data data/processed/$OHLCV_BASENAME"
    if [ -n "$EXEC_DATA_PATH" ]; then
        ENS_ART_ARGS="$ENS_ART_ARGS --exec-data $EXEC_DATA_PATH"
    fi
    if (( $(echo "$SLIPPAGE_PER_SIDE > 0" | bc -l) )); then
        ENS_ART_ARGS="$ENS_ART_ARGS --slippage-per-side $SLIPPAGE_PER_SIDE"
    fi
    python agent/generate_ensemble_artifacts.py $ENS_ART_ARGS 2>&1 | tee -a "$LOG"
    echo "  Ensemble backtest artifacts complete." | tee -a "$LOG"
else
    # --- [3b/5] SKIPPED (individual mode) ---
    echo "" | tee -a "$LOG"
    echo "[3b/5] Skipped — individual mode (no ensemble sweep/selection)" | tee -a "$LOG"

    # --- [4/5] Run batch_post_optimizer (individual mode — per-side Long/Short) ---
    echo "" | tee -a "$LOG"
    echo "[4/5] Running batch post-optimizer (individual mode — per-side Long/Short)..." | tee -a "$LOG"
    echo "  Command: python agent/batch_post_optimizer.py --batch-dir $BATCH_DIR --n-trials $N_TRIALS --holdout-months $HOLDOUT_MONTHS --workers $WORKERS --objective $OBJECTIVE --no-filter $OPT_ARGS" | tee -a "$LOG"

    python agent/batch_post_optimizer.py \
        --batch-dir "$BATCH_DIR" \
        --n-trials "$N_TRIALS" \
        --holdout-months "$HOLDOUT_MONTHS" \
        --workers "$WORKERS" \
        --objective "$OBJECTIVE" \
        --no-filter \
        $OPT_ARGS \
        2>&1 | tee -a "$LOG"

    # --- [4b/5] Unified Selection & Pairing Engine ---
    echo "" | tee -a "$LOG"
    echo "[4b/5] Running Unified Selection & Pairing Engine..." | tee -a "$LOG"
    python agent/unified_pair_optimizer.py --batch-dir "$BATCH_DIR" 2>&1 | tee -a "$LOG"

    # --- [4c/5] Run ensemble optimization on VM ---
    # Runs on the same VM that just completed individual optimization.
    # 4 pairs is lightweight (~5-10 min on the warm VM).
    TOP_PAIRS="$BATCH_DIR/top_pairs.json"
    if [ -f "$TOP_PAIRS" ]; then
        PAIR_COUNT=$(python3 -c "import json; print(len(json.load(open('$TOP_PAIRS'))))")
        if [ "$PAIR_COUNT" -eq 0 ]; then
            echo "  FATAL ERROR: unified_pair_optimizer.py produced 0 pairs! This indicates a failure in parsing or individual optimization. Aborting." | tee -a "$LOG"
            exit 1
        fi

        echo "" | tee -a "$LOG"
        echo "[4c/5] Running ensemble optimization on VM (top pairs)..." | tee -a "$LOG"

        EXEC_ENS_ARGS=""
        if [ -n "$EXEC_DATA_PATH" ]; then
            EXEC_ENS_ARGS="--exec-data $EXEC_DATA_PATH"
        fi
        if (( $(echo "$SLIPPAGE_PER_SIDE > 0" | bc -l) )); then
            EXEC_ENS_ARGS="$EXEC_ENS_ARGS --slippage-per-side $SLIPPAGE_PER_SIDE"
        fi
        EXEC_ENS_ARGS="$EXEC_ENS_ARGS --random-seed $RANDOM_SEED"

        python agent/batch_post_optimizer.py \
            --batch-dir "$BATCH_DIR" \
            --target-pairs-json "$TOP_PAIRS" \
            --n-trials "$N_TRIALS" \
            --holdout-months "$HOLDOUT_MONTHS" \
            --workers "$WORKERS" \
            --objective "$OBJECTIVE" \
            --no-filter \
            $EXEC_ENS_ARGS \
            2>&1 | tee -a "$LOG"

        # Generate ensemble backtest artifacts (markdown reports)
        echo "  Generating ensemble backtest artifacts..." | tee -a "$LOG"
        ENS_ART_ARGS="--batch-dir $BATCH_DIR --data data/processed/$OHLCV_BASENAME"
        if [ -n "$EXEC_DATA_PATH" ]; then
            ENS_ART_ARGS="$ENS_ART_ARGS --exec-data $EXEC_DATA_PATH"
        fi
        if (( $(echo "$SLIPPAGE_PER_SIDE > 0" | bc -l) )); then
            ENS_ART_ARGS="$ENS_ART_ARGS --slippage-per-side $SLIPPAGE_PER_SIDE"
        fi
        python agent/generate_ensemble_artifacts.py $ENS_ART_ARGS 2>&1 | tee -a "$LOG"
        echo "  Ensemble optimization complete." | tee -a "$LOG"
    else
        echo "  No top_pairs.json — skipping ensemble optimization." | tee -a "$LOG"
    fi
fi

OPT_EXIT=$?

# --- [4b/6] Generate correctly-formatted strategy configs ---
echo "" | tee -a "$LOG"
echo "[4b/6] Generating strategy configs from optimization results..." | tee -a "$LOG"

python agent/generate_batch_configs.py \
    --batch-dir "$BATCH_DIR" \
    --min-trades 10 \
    --objective "$OBJECTIVE" \
    2>&1 | tee -a "$LOG" || echo "  WARNING: Config generation failed (non-fatal)" | tee -a "$LOG"

# --- [5/6] Upload results to GCS ---
echo "" | tee -a "$LOG"
echo "[5/6] Uploading results to GCS..." | tee -a "$LOG"

# Upload optimized reports and results JSONs (glob for all objectives)
for f in "$BATCH_DIR"/batch_summary_optimized_*.md; do
    [ -f "$f" ] && gsutil cp "$f" "$BUCKET/$GCS_OPT_PREFIX/" 2>&1 | tee -a "$LOG" || true
done
for f in "$BATCH_DIR"/optimization_results_*.json; do
    [ -f "$f" ] && gsutil cp "$f" "$BUCKET/$GCS_OPT_PREFIX/" 2>&1 | tee -a "$LOG" || true
done
[ -f "$BATCH_DIR/batch_ensemble_pre_opt.md" ] && gsutil cp "$BATCH_DIR/batch_ensemble_pre_opt.md" "$BUCKET/$GCS_OPT_PREFIX/" 2>&1 | tee -a "$LOG" || true
[ -f "$BATCH_DIR/top_8_ensembles.json" ] && gsutil cp "$BATCH_DIR/top_8_ensembles.json" "$BUCKET/$GCS_OPT_PREFIX/" 2>&1 | tee -a "$LOG" || true
[ -f "$BATCH_DIR/top_pairs.json" ] && gsutil cp "$BATCH_DIR/top_pairs.json" "$BUCKET/$GCS_OPT_PREFIX/" 2>&1 | tee -a "$LOG" || true

# Legacy fallback uploads
[ -f "$BATCH_DIR/batch_summary_optimized.md" ] && gsutil cp "$BATCH_DIR/batch_summary_optimized.md" "$BUCKET/$GCS_OPT_PREFIX/" 2>&1 | tee -a "$LOG" || true
[ -f "$BATCH_DIR/optimization_results.json" ] && gsutil cp "$BATCH_DIR/optimization_results.json" "$BUCKET/$GCS_OPT_PREFIX/" 2>&1 | tee -a "$LOG" || true

# Upload all optimized config JSONs (legacy per-experiment configs)
find reports/ -name "*_opt.json" \( -path "*/canary_output/*" -o -path "*/production_output/*" \) -exec gsutil cp {} "$BUCKET/$GCS_OPT_PREFIX/configs/" \; 2>&1 | tee -a "$LOG" || true
find reports/ -name "*_opt_long.json" \( -path "*/canary_output/*" -o -path "*/production_output/*" \) -exec gsutil cp {} "$BUCKET/$GCS_OPT_PREFIX/configs/" \; 2>&1 | tee -a "$LOG" || true
find reports/ -name "*_opt_short.json" \( -path "*/canary_output/*" -o -path "*/production_output/*" \) -exec gsutil cp {} "$BUCKET/$GCS_OPT_PREFIX/configs/" \; 2>&1 | tee -a "$LOG" || true

# Upload generated batch configs (correctly-formatted with all top-level keys)
if [ -d "$BATCH_DIR/configs" ]; then
    gsutil -m cp "$BATCH_DIR/configs/*.json" "$BUCKET/$GCS_OPT_PREFIX/batch_configs/" 2>&1 | tee -a "$LOG" || true
    echo "  Uploaded batch configs" | tee -a "$LOG"
fi

# Upload ensemble backtest artifacts (MDs, JSONs, predictions)
for f in "$BATCH_DIR"/*ensemble_backtests*.md; do
    [ -f "$f" ] && gsutil cp "$f" "$BUCKET/$GCS_OPT_PREFIX/" 2>&1 | tee -a "$LOG" || true
done
# Upload all JSON configs from batch dir (includes optimized ensemble configs)
for f in "$BATCH_DIR"/*.json; do
    [ -f "$f" ] && gsutil cp "$f" "$BUCKET/$GCS_OPT_PREFIX/" 2>&1 | tee -a "$LOG" || true
done
# Upload ensemble predictions directory
if [ -d "$BATCH_DIR/predictions" ]; then
    gsutil -m cp -r "$BATCH_DIR/predictions" "$BUCKET/$GCS_OPT_PREFIX/" 2>&1 | tee -a "$LOG" || true
    echo "  Uploaded ensemble predictions" | tee -a "$LOG"
fi

# Upload log
gsutil cp "$LOG" "$BUCKET/$GCS_OPT_PREFIX/logs/" 2>/dev/null || true

TOTAL_ELAPSED=$(( $(date +%s) - START_TIME ))
echo "" | tee -a "$LOG"
echo "============================================================" | tee -a "$LOG"
echo " POST-OPTIMIZER COMPLETE" | tee -a "$LOG"
echo "  Exit code:     $OPT_EXIT" | tee -a "$LOG"
echo "  Wall time:     $((TOTAL_ELAPSED / 3600))h $((TOTAL_ELAPSED % 3600 / 60))m" | tee -a "$LOG"
echo "  GCS results:   $BUCKET/$GCS_OPT_PREFIX/" | tee -a "$LOG"
echo "  Batch configs: $BATCH_DIR/configs/" | tee -a "$LOG"
echo "============================================================" | tee -a "$LOG"

