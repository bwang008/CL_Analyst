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

set -eo pipefail

export PYTHONIOENCODING=utf8

# Activate environment
source /opt/optuna-env/bin/activate

PROJECT_DIR="/home/$(whoami)/project"
cd "$PROJECT_DIR"

# Defaults
BATCH_ID=""
N_TRIALS=1000
HOLDOUT_MONTHS=4
WORKERS=24
SHUTDOWN=false
BUCKET="gs://cltrainer-optuna-results"
LOG="post_optimize_$(date +%Y%m%d_%H%M%S).log"

# Parse args
for arg in "$@"; do
    case "$arg" in
        --batch-id=*) BATCH_ID="${arg#*=}" ;;
        --n-trials=*) N_TRIALS="${arg#*=}" ;;
        --holdout-months=*) HOLDOUT_MONTHS="${arg#*=}" ;;
        --workers=*) WORKERS="${arg#*=}" ;;
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
echo "  Bucket:        $BUCKET" | tee -a "$LOG"
echo "  CPUs:          $(nproc)" | tee -a "$LOG"
echo "============================================================" | tee -a "$LOG"

# --- [1/5] Download batch metadata ---
echo "" | tee -a "$LOG"
echo "[1/5] Downloading batch metadata from GCS..." | tee -a "$LOG"
BATCH_DIR="reports/batch_runs/$BATCH_ID"
mkdir -p "$BATCH_DIR"
gsutil cp "$BUCKET/$GCS_OPT_PREFIX/batch_progress.json" "$BATCH_DIR/batch_progress.json" 2>&1 | tee -a "$LOG"
gsutil cp "$BUCKET/$GCS_OPT_PREFIX/manifest.json" "$BATCH_DIR/manifest.json" 2>&1 | tee -a "$LOG"

# Extract OHLCV GCS path from manifest
OHLCV_GCS=$(python3 -c "import json; m=json.load(open('$BATCH_DIR/manifest.json')); print(m.get('defaults',{}).get('gcs_data_path',''))")
OHLCV_BASENAME=$(basename "$OHLCV_GCS")
echo "  OHLCV GCS path: $OHLCV_GCS" | tee -a "$LOG"

if [ -z "$OHLCV_GCS" ]; then
    echo "  ERROR: No gcs_data_path found in manifest!" | tee -a "$LOG"
    gsutil cp "$LOG" "$BUCKET/$GCS_OPT_PREFIX/logs/" 2>/dev/null || true
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
    gsutil cp "$LOG" "$BUCKET/$GCS_OPT_PREFIX/logs/" 2>/dev/null || true
    exit 1
fi

# --- [4/5] Run batch_post_optimizer ---
echo "" | tee -a "$LOG"
echo "[4/5] Running batch post-optimizer..." | tee -a "$LOG"
echo "  Command: python agent/batch_post_optimizer.py --batch-dir $BATCH_DIR --n-trials $N_TRIALS --holdout-months $HOLDOUT_MONTHS --workers $WORKERS --no-filter" | tee -a "$LOG"

python agent/batch_post_optimizer.py \
    --batch-dir "$BATCH_DIR" \
    --n-trials "$N_TRIALS" \
    --holdout-months "$HOLDOUT_MONTHS" \
    --workers "$WORKERS" \
    --no-filter \
    2>&1 | tee -a "$LOG"

OPT_EXIT=$?

# --- [5/5] Upload results to GCS ---
echo "" | tee -a "$LOG"
echo "[5/5] Uploading results to GCS..." | tee -a "$LOG"

# Upload optimized report and results JSON
gsutil cp "$BATCH_DIR/batch_summary_optimized.md" "$BUCKET/$GCS_OPT_PREFIX/" 2>&1 | tee -a "$LOG" || true
gsutil cp "$BATCH_DIR/optimization_results.json" "$BUCKET/$GCS_OPT_PREFIX/" 2>&1 | tee -a "$LOG" || true

# Upload all optimized config JSONs
find reports/ -name "*_opt.json" -path "*/canary_output/*" -exec gsutil cp {} "$BUCKET/$GCS_OPT_PREFIX/configs/" \; 2>&1 | tee -a "$LOG" || true
find reports/ -name "*_opt_long.json" -path "*/canary_output/*" -exec gsutil cp {} "$BUCKET/$GCS_OPT_PREFIX/configs/" \; 2>&1 | tee -a "$LOG" || true
find reports/ -name "*_opt_short.json" -path "*/canary_output/*" -exec gsutil cp {} "$BUCKET/$GCS_OPT_PREFIX/configs/" \; 2>&1 | tee -a "$LOG" || true

# Upload log
gsutil cp "$LOG" "$BUCKET/$GCS_OPT_PREFIX/logs/" 2>/dev/null || true

TOTAL_ELAPSED=$(( $(date +%s) - START_TIME ))
echo "" | tee -a "$LOG"
echo "============================================================" | tee -a "$LOG"
echo " POST-OPTIMIZER COMPLETE" | tee -a "$LOG"
echo "  Exit code:     $OPT_EXIT" | tee -a "$LOG"
echo "  Wall time:     $((TOTAL_ELAPSED / 3600))h $((TOTAL_ELAPSED % 3600 / 60))m" | tee -a "$LOG"
echo "  GCS results:   $BUCKET/$GCS_OPT_PREFIX/" | tee -a "$LOG"
echo "============================================================" | tee -a "$LOG"

# Final log upload
gsutil cp "$LOG" "$BUCKET/$GCS_OPT_PREFIX/logs/" 2>/dev/null || true

# Shutdown VM if requested
if [ "$SHUTDOWN" = true ]; then
    echo "Shutting down optimizer VM in 15 seconds..." | tee -a "$LOG"
    gsutil cp "$LOG" "$BUCKET/$GCS_OPT_PREFIX/logs/" 2>/dev/null || true
    sleep 15
    sudo shutdown -h now
fi
