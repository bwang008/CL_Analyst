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

export PYTHONIOENCODING=utf8

cleanup() {
    local exit_code=$?
    if [ $exit_code -ne 0 ]; then
        echo "============================================================" | tee -a "$LOG"
        echo " TRAP TRIGGERED: Script exited with code $exit_code" | tee -a "$LOG"
        echo "============================================================" | tee -a "$LOG"
        
        # Send Telegram alert
        python3 -c "
import os, sys, urllib.request, json
try:
    with open('/home/$(whoami)/project/.env') as f:
        env = dict(line.strip().split('=', 1) for line in f if '=' in line and not line.startswith('#'))
    token = env.get('TELEGRAM_BOT_TOKEN')
    chat_id = env.get('TELEGRAM_CHAT_ID')
    if token and chat_id:
        batch = sys.argv[4] if len(sys.argv) > 4 else 'unknown'
        msg = f'🚨 <b>[VM CRASH] Post-Optimizer</b>\nBatch: <code>{batch}</code>\nExit Code: {sys.argv[2]}\n\n<b>Log Tail:</b>\n<pre>{sys.argv[3]}</pre>'
        req = urllib.request.Request(f'https://api.telegram.org/bot{token}/sendMessage', 
            data=json.dumps({'chat_id': chat_id, 'text': msg, 'parse_mode': 'HTML'}).encode('utf-8'),
            headers={'Content-Type': 'application/json'})
        urllib.request.urlopen(req, timeout=10)
except Exception as e:
    print('Failed to send telegram:', e)
" "$0" "$exit_code" "$(tail -n 15 "$LOG" 2>/dev/null)" "$BATCH_ID" || true
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
HOLDOUT_MONTHS=4
WORKERS=0
SHUTDOWN=false
OBJECTIVE="both"
SWEEP_MODE="backtest"
OPT_MODE="individual"
BUCKET="gs://cltrainer-optuna-results"
LOG="post_optimize_$(date +%Y%m%d_%H%M%S).log"

# Parse args
for arg in "$@"; do
    case "$arg" in
        --batch-id=*) BATCH_ID="${arg#*=}" ;;
        --n-trials=*) N_TRIALS="${arg#*=}" ;;
        --holdout-months=*) HOLDOUT_MONTHS="${arg#*=}" ;;
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
echo "  Holdout:       $HOLDOUT_MONTHS months" | tee -a "$LOG"
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

# Extract OHLCV GCS path from manifest
OHLCV_GCS=$(python3 -c "import json; m=json.load(open('$BATCH_DIR/manifest.json')); print(m.get('defaults',{}).get('gcs_data_path',''))")
OHLCV_BASENAME=$(basename "$OHLCV_GCS")
STRATEGY_CONFIG=$(python3 -c "import json; m=json.load(open('$BATCH_DIR/manifest.json')); print(m.get('defaults',{}).get('strategy_config','hourly_ensemble_010.json'))")
echo "  OHLCV GCS path: $OHLCV_GCS" | tee -a "$LOG"
echo "  Strategy config: $STRATEGY_CONFIG" | tee -a "$LOG"

if [ -z "$OHLCV_GCS" ]; then
    echo "  ERROR: No gcs_data_path found in manifest!" | tee -a "$LOG"
    exit 1
fi

# --- [2/5] Download OHLCV data ---
echo "" | tee -a "$LOG"
echo "[2/5] Downloading OHLCV data from GCS..." | tee -a "$LOG"
mkdir -p data/processed
gsutil cp "$OHLCV_GCS" "data/processed/$OHLCV_BASENAME" 2>&1 | tee -a "$LOG"
echo "  OHLCV data ready ($(du -h data/processed/$OHLCV_BASENAME | cut -f1))" | tee -a "$LOG"

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
    canary_dir = os.path.join(local_dir, 'registry', 'canary_output')
    os.makedirs(canary_dir, exist_ok=True)
    
    # Download production artifacts zip
    gcs_zip = f'{bucket}/{prefix}/production/'
    print(f'  Downloading artifacts for {prefix}...')
    subprocess.run(['gsutil', '-m', 'cp', '-r', f'{gcs_zip}*.zip', local_dir], 
                    capture_output=True)
    
    # Unzip into registry/canary_output
    import glob
    for zf in glob.glob(os.path.join(local_dir, '*.zip')):
        subprocess.run(['unzip', '-o', '-q', zf, '-d', os.path.join(local_dir, 'registry')],
                        capture_output=True)
        print(f'    Unpacked: {os.path.basename(zf)}')
    
    # Also download pipeline_summary.json
    subprocess.run(['gsutil', 'cp', f'{bucket}/{prefix}/pipeline_summary.json', 
                     os.path.join(local_dir, 'pipeline_summary.json')], capture_output=True)
    # Copy to canary_output too for compatibility
    summary_src = os.path.join(local_dir, 'pipeline_summary.json')
    if os.path.exists(summary_src):
        import shutil
        shutil.copy2(summary_src, canary_dir)
    
    # Update local_dir in batch_progress to Linux path
    exp['local_dir'] = local_dir

# Rewrite batch_progress.json with Linux paths
with open('$BATCH_DIR/batch_progress.json', 'w') as f:
    json.dump(bp, f, indent=2)
print(f'  Updated batch_progress.json with {len(experiments)} experiments (Linux paths)')
" 2>&1 | tee -a "$LOG"

# Verify artifacts
ARTIFACT_COUNT=$(find reports/ -name "*.csv" -path "*/canary_output/*" | wc -l)
echo "  Found $ARTIFACT_COUNT prediction CSVs" | tee -a "$LOG"

CONFIG_COUNT=$(find reports/ -name "*.json" -path "*/canary_output/*" -not -name "pipeline_*" | wc -l)
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

    # --- [4/5] Run batch_post_optimizer (ensemble mode) ---
    echo "" | tee -a "$LOG"
    echo "[4/5] Running batch post-optimizer on Top 8 (ensemble mode)..." | tee -a "$LOG"
    echo "  Command: python agent/batch_post_optimizer.py --batch-dir $BATCH_DIR --target-pairs-json $BATCH_DIR/top_8_ensembles.json --n-trials $N_TRIALS --holdout-months $HOLDOUT_MONTHS --workers $WORKERS --objective $OBJECTIVE --no-filter" | tee -a "$LOG"

    python agent/batch_post_optimizer.py \
        --batch-dir "$BATCH_DIR" \
        --target-pairs-json "$BATCH_DIR/top_8_ensembles.json" \
        --n-trials "$N_TRIALS" \
        --holdout-months "$HOLDOUT_MONTHS" \
        --workers "$WORKERS" \
        --objective "$OBJECTIVE" \
        --no-filter \
        2>&1 | tee -a "$LOG"
else
    # --- [3b/5] SKIPPED (individual mode) ---
    echo "" | tee -a "$LOG"
    echo "[3b/5] Skipped — individual mode (no ensemble sweep/selection)" | tee -a "$LOG"

    # --- [4/5] Run batch_post_optimizer (individual mode — per-side Long/Short) ---
    echo "" | tee -a "$LOG"
    echo "[4/5] Running batch post-optimizer (individual mode — per-side Long/Short)..." | tee -a "$LOG"
    echo "  Command: python agent/batch_post_optimizer.py --batch-dir $BATCH_DIR --n-trials $N_TRIALS --holdout-months $HOLDOUT_MONTHS --workers $WORKERS --objective $OBJECTIVE --no-filter" | tee -a "$LOG"

    python agent/batch_post_optimizer.py \
        --batch-dir "$BATCH_DIR" \
        --n-trials "$N_TRIALS" \
        --holdout-months "$HOLDOUT_MONTHS" \
        --workers "$WORKERS" \
        --objective "$OBJECTIVE" \
        --no-filter \
        2>&1 | tee -a "$LOG"

    # --- [4b/5] Unified Selection & Pairing Engine ---
    echo "" | tee -a "$LOG"
    echo "[4b/5] Running Unified Selection & Pairing Engine..." | tee -a "$LOG"
    python agent/unified_pair_optimizer.py --batch-dir "$BATCH_DIR" 2>&1 | tee -a "$LOG"

    if [ -f "$BATCH_DIR/top_pairs.json" ]; then
        echo "" | tee -a "$LOG"
        echo "[4c/5] Running batch post-optimizer on unified pairs (ensemble mode)..." | tee -a "$LOG"
        python agent/batch_post_optimizer.py \
            --batch-dir "$BATCH_DIR" \
            --target-pairs-json "$BATCH_DIR/top_pairs.json" \
            --n-trials "$N_TRIALS" \
            --holdout-months "$HOLDOUT_MONTHS" \
            --workers "$WORKERS" \
            --objective "both" \
            --no-filter \
            2>&1 | tee -a "$LOG"
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
find reports/ -name "*_opt.json" -path "*/canary_output/*" -exec gsutil cp {} "$BUCKET/$GCS_OPT_PREFIX/configs/" \; 2>&1 | tee -a "$LOG" || true
find reports/ -name "*_opt_long.json" -path "*/canary_output/*" -exec gsutil cp {} "$BUCKET/$GCS_OPT_PREFIX/configs/" \; 2>&1 | tee -a "$LOG" || true
find reports/ -name "*_opt_short.json" -path "*/canary_output/*" -exec gsutil cp {} "$BUCKET/$GCS_OPT_PREFIX/configs/" \; 2>&1 | tee -a "$LOG" || true

# Upload generated batch configs (correctly-formatted with all top-level keys)
if [ -d "$BATCH_DIR/configs" ]; then
    gsutil -m cp "$BATCH_DIR/configs/*.json" "$BUCKET/$GCS_OPT_PREFIX/batch_configs/" 2>&1 | tee -a "$LOG" || true
    echo "  Uploaded batch configs" | tee -a "$LOG"
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

