#!/bin/bash
# =============================================================================
# E2E Alpha Factory — Sweep (Production) Run
#
# A parameterized version of vm_canary_run.sh meant for deep, exhaustive optimization.
#   - 4 searches (2 metrics × 2 directions)
#   - Default 300 trials per search
#   - Expanded search space (deep trees, max 1500 estimators)
#   - Outputs to gs://cltrainer-optuna-results/<job-name>/ and vaults upon completion.
#
# Usage (called by gcp_deploy_sweep.ps1 or manually):
#   bash gcp/vm_sweep_run.sh [--shutdown] [--dataset=<name>] [--metrics=<list>] [--job-name=<name>] [--n-trials=<N>]
#
# Arguments:
#   --dataset=<name>   Dataset filename without path/extension (default: cl-5m_bk_set_10)
#   --metrics=<list>   Comma-separated metrics to search (default: logloss,average_precision)
#   --shutdown         Auto-shutdown VM after completion
#   --agent-id=<id>    Agent identifier for logging
# =============================================================================

set -eo pipefail

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
        msg = f'🚨 <b>[VM CRASH] {os.path.basename(sys.argv[1])}</b>\nExit Code: {sys.argv[2]}\n\n<b>Log Tail:</b>\n<pre>{sys.argv[3]}</pre>'
        req = urllib.request.Request(f'https://api.telegram.org/bot{token}/sendMessage', 
            data=json.dumps({'chat_id': chat_id, 'text': msg, 'parse_mode': 'HTML'}).encode('utf-8'),
            headers={'Content-Type': 'application/json'})
        urllib.request.urlopen(req, timeout=10)
except Exception as e:
    print('Failed to send telegram:', e)
" "$0" "$exit_code" "$(tail -n 15 "$LOG" 2>/dev/null)" || true
    fi
    
    # Upload logs before dying
    if [ -n "$JOB_NAME" ] && [ -n "$LOG" ]; then
        echo "Uploading final logs to $BUCKET/$JOB_NAME/logs/"
        gsutil cp "$LOG" "$BUCKET/$JOB_NAME/logs/" 2>/dev/null || true
    fi

    # Delete VM if requested
    if [ "$SHUTDOWN" = true ]; then
        echo "Self-deleting VM in 15 seconds..." | tee -a "$LOG"
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

# Configuration — SWEEP OVERRIDES
DATASET_NAME="cl-5m_bk_set_10"
METRICS="logloss,average_precision"
TARGET_LONG="TARGET_TRIPLE_2x1_24H_LONG"
TARGET_SHORT="TARGET_TRIPLE_2x1_24H_SHORT"
CUTOFF="2022-01-01"
N_TRIALS=200
N_WORKERS=4
THREADS_PER_WORKER=$(( $(nproc) / N_WORKERS ))
DB_DIR="models/optuna_studies"
BUCKET="gs://cltrainer-optuna-results"
JOB_NAME=""  # Auto-derived from DATASET_NAME below if not set via --job-name
STRATEGY="configs/strategies/ensemble4.json"
LOG="sweep_run_$(date +%Y%m%d_%H%M%S).log"
SHUTDOWN=false
USE_BUCKETS=false
AGENT_ID="${AGENT_ID:-sweep_bot}"
EXEC_DATA=""
SLIPPAGE_PER_SIDE="0"

# Search space constraints (defaults — overridden by manifest via CLI)
MAX_DEPTH_MIN=3
MAX_DEPTH_MAX=10
NUM_LEAVES_MIN=15
NUM_LEAVES_MAX=100
MAX_N_ESTIMATORS=2000
EARLY_STOPPING=25
MAX_FOLDS=5
LEARNING_RATE_MIN=0.005
LEARNING_RATE_MAX=0.02
MIN_CHILD_SAMPLES_MIN=150
MIN_CHILD_SAMPLES_MAX=400
FEATURE_FRACTION_MIN=0.3
FEATURE_FRACTION_MAX=1.0

# Parse args
for arg in "$@"; do
    case "$arg" in
        --shutdown) SHUTDOWN=true ;;
        --agent-id=*) AGENT_ID="${arg#*=}" ;;
        --dataset=*) DATASET_NAME="${arg#*=}" ;;
        --metrics=*) METRICS="${arg#*=}" ;;
        --target-long=*) TARGET_LONG="${arg#*=}" ;;
        --target-short=*) TARGET_SHORT="${arg#*=}" ;;
        --strategy=*) STRATEGY="configs/strategies/${arg#*=}" ;;
        --job-name=*) JOB_NAME="${arg#*=}" ;;
        --n-trials=*) N_TRIALS="${arg#*=}" ;;
        --use-buckets) USE_BUCKETS=true ;;
        --max-depth-min=*) MAX_DEPTH_MIN="${arg#*=}" ;;
        --max-depth-max=*) MAX_DEPTH_MAX="${arg#*=}" ;;
        --num-leaves-min=*) NUM_LEAVES_MIN="${arg#*=}" ;;
        --num-leaves-max=*) NUM_LEAVES_MAX="${arg#*=}" ;;
        --max-n-estimators=*) MAX_N_ESTIMATORS="${arg#*=}" ;;
        --early-stopping=*) EARLY_STOPPING="${arg#*=}" ;;
        --max-folds=*) MAX_FOLDS="${arg#*=}" ;;
        --learning-rate-min=*) LEARNING_RATE_MIN="${arg#*=}" ;;
        --learning-rate-max=*) LEARNING_RATE_MAX="${arg#*=}" ;;
        --min-child-samples-min=*) MIN_CHILD_SAMPLES_MIN="${arg#*=}" ;;
        --min-child-samples-max=*) MIN_CHILD_SAMPLES_MAX="${arg#*=}" ;;
        --feature-fraction-min=*) FEATURE_FRACTION_MIN="${arg#*=}" ;;
        --feature-fraction-max=*) FEATURE_FRACTION_MAX="${arg#*=}" ;;
        --exec-data=*) EXEC_DATA="${arg#*=}" ;;
        --slippage-per-side=*) SLIPPAGE_PER_SIDE="${arg#*=}" ;;
    esac
done

# Auto-derive JOB_NAME from DATASET_NAME if not explicitly set
if [ -z "$JOB_NAME" ]; then
    # Strip common prefixes to get a clean name, e.g. cl-5m_bk_set_11c -> sweep_set_11c
    CLEAN_NAME=$(echo "$DATASET_NAME" | sed 's/^cl-[0-9]*[mh]_bk_//')
    JOB_NAME="sweep_${CLEAN_NAME}"
    echo "Auto-derived JOB_NAME: $JOB_NAME"
fi

# Resolve DATA path from DATASET_NAME
DATA="/home/$(whoami)/data/${DATASET_NAME}.parquet"

# ---------- CPU VALIDATION ----------
SYSTEM_CPUS=$(nproc)
REQUIRED_CPUS=$((N_WORKERS * THREADS_PER_WORKER))
if [ "$REQUIRED_CPUS" -ne "$SYSTEM_CPUS" ]; then
    echo "" | tee "$LOG"
    echo "FATAL: CPU mismatch!" | tee -a "$LOG"
    echo "  System CPUs:   $SYSTEM_CPUS" | tee -a "$LOG"
    echo "  Required:      $REQUIRED_CPUS (N_WORKERS=$N_WORKERS × THREADS_PER_WORKER=$THREADS_PER_WORKER)" | tee -a "$LOG"
    echo "  Fix: adjust N_WORKERS and THREADS_PER_WORKER in this script to match the machine." | tee -a "$LOG"
    echo "" | tee -a "$LOG"
    exit 1
fi
# No TRIALS_PER_WORKER needed — each search runs the full N_TRIALS sequentially

# Build search combos from METRICS arg
COMBOS=()
IFS=',' read -ra METRIC_LIST <<< "$METRICS"
for metric in "${METRIC_LIST[@]}"; do
    COMBOS+=("$TARGET_LONG $metric")
    COMBOS+=("$TARGET_SHORT $metric")
done

TOTAL=${#COMBOS[@]}
COMPLETED=0
FAILED=0
START_TIME=$(date +%s)

echo "============================================================" | tee "$LOG"
echo " E2E ALPHA FACTORY — SWEEP (PRODUCTION) RUN" | tee -a "$LOG"
echo "============================================================" | tee -a "$LOG"
echo "  Timestamp:  $(date -Iseconds)" | tee -a "$LOG"
echo "  Agent:      $AGENT_ID" | tee -a "$LOG"
echo "  Hostname:   $(hostname)" | tee -a "$LOG"
echo "  CPUs:       $SYSTEM_CPUS ($TOTAL parallel searches × $THREADS_PER_WORKER threads each)" | tee -a "$LOG"
echo "  Data:       $DATA" | tee -a "$LOG"
echo "  Cutoff:     $CUTOFF" | tee -a "$LOG"
echo "  Trials:     $N_TRIALS per search (sequential Bayesian optimization)" | tee -a "$LOG"
echo "  Parallelism: $TOTAL searches run simultaneously, each with n_jobs=1" | tee -a "$LOG"
echo "  Searches:   $TOTAL (${#METRIC_LIST[@]} metrics × 2 directions)" | tee -a "$LOG"
echo "  Strategy:   $STRATEGY" | tee -a "$LOG"
echo "  Shutdown:   $SHUTDOWN" | tee -a "$LOG"
echo "  Log:        $LOG" | tee -a "$LOG"
echo "  GCS dest:   $BUCKET/$JOB_NAME/" | tee -a "$LOG"
echo "  Buckets:    $USE_BUCKETS" | tee -a "$LOG"
echo "" | tee -a "$LOG"
echo "  SEARCH SPACE CONSTRAINTS:" | tee -a "$LOG"
echo "    max_depth:          [$MAX_DEPTH_MIN, $MAX_DEPTH_MAX]" | tee -a "$LOG"
echo "    num_leaves:         [$NUM_LEAVES_MIN, $NUM_LEAVES_MAX]" | tee -a "$LOG"
echo "    n_estimators:       max $MAX_N_ESTIMATORS" | tee -a "$LOG"
echo "    early_stopping:     $EARLY_STOPPING rounds" | tee -a "$LOG"
echo "    learning_rate:      [$LEARNING_RATE_MIN, $LEARNING_RATE_MAX]" | tee -a "$LOG"
echo "    min_child_samples:  [$MIN_CHILD_SAMPLES_MIN, $MIN_CHILD_SAMPLES_MAX]" | tee -a "$LOG"
echo "    feature_fraction:   [$FEATURE_FRACTION_MIN, $FEATURE_FRACTION_MAX]" | tee -a "$LOG"
echo "    max_folds:          $MAX_FOLDS" | tee -a "$LOG"
echo "============================================================" | tee -a "$LOG"
echo "" | tee -a "$LOG"

# =============================================================================
# PHASE 1: Launch ALL searches in parallel (one process per search)
# =============================================================================

echo "" | tee -a "$LOG"
echo "============================================================" | tee -a "$LOG"
echo " Launching $TOTAL searches in parallel..." | tee -a "$LOG"
echo "============================================================" | tee -a "$LOG"

# Clean up stale journal files
for i in "${!COMBOS[@]}"; do
    combo="${COMBOS[$i]}"
    read -r TARGET METRIC <<< "$combo"
    if [[ "$TARGET" == *"LONG" ]]; then DIR="long"; else DIR="short"; fi
    STUDY="${JOB_NAME}_${DIR}_${METRIC}"
    if [ -f "${DB_DIR}/${STUDY}.journal" ]; then
        echo "  Removing stale journal: ${DB_DIR}/${STUDY}.journal" | tee -a "$LOG"
        rm -f "${DB_DIR}/${STUDY}.journal"
    fi
done

# Clean up stale worker status files
rm -f /tmp/worker_W*_status.json 2>/dev/null || true

# Launch each search as an independent background process
SEARCH_PIDS=()
SEARCH_LABELS=()
for i in "${!COMBOS[@]}"; do
    combo="${COMBOS[$i]}"
    read -r TARGET METRIC <<< "$combo"
    if [[ "$TARGET" == *"LONG" ]]; then DIR="long"; else DIR="short"; fi
    STUDY="${JOB_NAME}_${DIR}_${METRIC}"
    WORKER_ID=$((i + 1))
    LABEL="${DIR^^} ${METRIC}"

    python agent/optuna_lgbm_search_v2.py \
        --target "$TARGET" \
        --data "$DATA" \
        --ml-metric "$METRIC" \
        --n-trials "$N_TRIALS" \
        --n-jobs 1 \
        --study-name "$STUDY" \
        --db-dir "$DB_DIR" \
        --train-cutoff-date "$CUTOFF" \
        --max-depth-range $MAX_DEPTH_MIN $MAX_DEPTH_MAX \
        --num-leaves-range $NUM_LEAVES_MIN $NUM_LEAVES_MAX \
        --max-n-estimators $MAX_N_ESTIMATORS \
        --early-stopping-rounds $EARLY_STOPPING \
        --learning-rate-range $LEARNING_RATE_MIN $LEARNING_RATE_MAX \
        --min-child-samples-range $MIN_CHILD_SAMPLES_MIN $MIN_CHILD_SAMPLES_MAX \
        --feature-fraction-range $FEATURE_FRACTION_MIN $FEATURE_FRACTION_MAX \
        --num-threads $THREADS_PER_WORKER \
        --max-folds $MAX_FOLDS \
        --worker-id $WORKER_ID \
        $([ "$USE_BUCKETS" = true ] && echo "--use-buckets") \
        2>&1 | tee -a "$LOG" &
    SEARCH_PIDS+=($!)
    SEARCH_LABELS+=("$LABEL")
    echo "  Started search W${WORKER_ID}: $LABEL (PID $!, study=$STUDY, $N_TRIALS trials)" | tee -a "$LOG"
done

echo "" | tee -a "$LOG"
echo "  All $TOTAL searches launched — waiting for completion..." | tee -a "$LOG"

# Wait for all searches and capture exit codes
for idx in "${!SEARCH_PIDS[@]}"; do
    wait ${SEARCH_PIDS[$idx]}
    EXIT_CODE=$?
    WID=$((idx + 1))
    LABEL="${SEARCH_LABELS[$idx]}"

    # Extract direction and metric for GCS uploads
    combo="${COMBOS[$idx]}"
    read -r TARGET METRIC <<< "$combo"
    if [[ "$TARGET" == *"LONG" ]]; then DIR="long"; else DIR="short"; fi
    STUDY="${JOB_NAME}_${DIR}_${METRIC}"

    if [ $EXIT_CODE -eq 0 ]; then
        COMPLETED=$((COMPLETED + 1))
        echo "  ✓ Search W${WID} PASSED ($LABEL)" | tee -a "$LOG"
    else
        FAILED=$((FAILED + 1))
        echo "  ✗ Search W${WID} FAILED ($LABEL, exit $EXIT_CODE)" | tee -a "$LOG"
    fi

    # Upload results for this search
    gsutil -m cp ${DB_DIR}/${STUDY}.journal "$BUCKET/$JOB_NAME/studies/" 2>/dev/null || true
    gsutil -m cp reports/optuna_*_${DIR}_${METRIC}.* "$BUCKET/$JOB_NAME/reports/" 2>/dev/null || true

    # Upload STATUS.json
    echo "{\"completed\": $COMPLETED, \"failed\": $FAILED, \"total\": $TOTAL, \"current\": \"${DIR}_${METRIC}\", \"agent\": \"$AGENT_ID\", \"last_update\": \"$(date -Iseconds)\"}" | \
        gsutil cp - "$BUCKET/$JOB_NAME/STATUS.json" 2>/dev/null || true
done

# Upload log after all searches
gsutil cp "$LOG" "$BUCKET/$JOB_NAME/logs/" 2>/dev/null || true

SEARCH_ELAPSED=$(( $(date +%s) - START_TIME ))
echo "" | tee -a "$LOG"
echo "============================================================" | tee -a "$LOG"
echo " ALL CANARY SEARCHES COMPLETE" | tee -a "$LOG"
echo "  Completed: ${COMPLETED}/${TOTAL}" | tee -a "$LOG"
echo "  Failed:    ${FAILED}/${TOTAL}" | tee -a "$LOG"
echo "  Elapsed:   $((SEARCH_ELAPSED / 3600))h $((SEARCH_ELAPSED % 3600 / 60))m" | tee -a "$LOG"
echo "============================================================" | tee -a "$LOG"

# =============================================================================
# PHASE 2: Run E2E pipeline (train + backtest + package)
# =============================================================================

if [ $COMPLETED -gt 0 ]; then
    echo "" | tee -a "$LOG"
    echo "============================================================" | tee -a "$LOG"
    echo " [PHASE 2] E2E PIPELINE — Train + Backtest + Package" | tee -a "$LOG"
    echo "============================================================" | tee -a "$LOG"
    echo "" | tee -a "$LOG"

    E2E_ARGS=(
        --data "$DATA"
        --train-cutoff-date "$CUTOFF"
        --strategy-config "$STRATEGY"
        --db-dir "$DB_DIR"
        --output-dir "${PROJECT_DIR}/canary_output"
        --gcs-bucket "$BUCKET"
        --gcs-prefix "$JOB_NAME"
        --metrics logloss average_precision
        --study-prefix "$JOB_NAME"
        --targets "$TARGET_LONG" "$TARGET_SHORT"
    )

    if [ -n "$EXEC_DATA" ]; then
        E2E_ARGS+=(--exec-data "$EXEC_DATA")
    fi
    if (( $(echo "$SLIPPAGE_PER_SIDE > 0" | bc -l) )); then
        E2E_ARGS+=(--slippage-per-side "$SLIPPAGE_PER_SIDE")
    fi

    python gcp/vm_e2e_pipeline.py "${E2E_ARGS[@]}" 2>&1 | tee -a "$LOG" || true
    E2E_EXIT=${PIPESTATUS[0]}

    if [ $E2E_EXIT -eq 0 ]; then
        echo "  ✓ E2E pipeline completed successfully" | tee -a "$LOG"

        # =====================================================================
        # VAULT: Copy artifacts to timestamped GCS prefix (never overwritten)
        # =====================================================================
        VAULT_TS=$(date +%Y%m%d_%H%M%S)
        VAULT_PREFIX="${JOB_NAME}_vault/${VAULT_TS}"
        echo "" | tee -a "$LOG"
        echo "  📦 Vaulting artifacts to gs://${BUCKET#gs://}/${VAULT_PREFIX}/" | tee -a "$LOG"
        gsutil -m cp -r "$BUCKET/$JOB_NAME/production/*" "$BUCKET/$VAULT_PREFIX/production/" 2>/dev/null || true
        gsutil -m cp -r "$BUCKET/$JOB_NAME/studies/*" "$BUCKET/$VAULT_PREFIX/studies/" 2>/dev/null || true
        gsutil -m cp -r "$BUCKET/$JOB_NAME/reports/*" "$BUCKET/$VAULT_PREFIX/reports/" 2>/dev/null || true
        gsutil cp "$LOG" "$BUCKET/$VAULT_PREFIX/logs/" 2>/dev/null || true
        echo "  ✓ Vaulted to $BUCKET/$VAULT_PREFIX/" | tee -a "$LOG"
    else
        echo "  ✗ E2E pipeline failed (exit $E2E_EXIT)" | tee -a "$LOG"
        exit $E2E_EXIT
    fi
else
    echo "  ⚠ Skipping E2E pipeline — no searches completed." | tee -a "$LOG"
fi

TOTAL_ELAPSED=$(( $(date +%s) - START_TIME ))
echo "" | tee -a "$LOG"
echo "============================================================" | tee -a "$LOG"
echo " CANARY RUN COMPLETE" | tee -a "$LOG"
echo "  Total wall time: $((TOTAL_ELAPSED / 3600))h $((TOTAL_ELAPSED % 3600 / 60))m" | tee -a "$LOG"
echo "  Searches:        ${COMPLETED}/${TOTAL} passed" | tee -a "$LOG"
echo "  GCS results:     $BUCKET/$JOB_NAME/" | tee -a "$LOG"
echo "============================================================" | tee -a "$LOG"
echo "" | tee -a "$LOG"
echo "Download results:" | tee -a "$LOG"
echo "  gsutil -m cp -r ${BUCKET}/${JOB_NAME}/* ." | tee -a "$LOG"
echo "" | tee -a "$LOG"
