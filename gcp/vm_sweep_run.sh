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

# Activate environment
source /opt/optuna-env/bin/activate

PROJECT_DIR="/home/$(whoami)/project"
cd "$PROJECT_DIR"

# Configuration — SWEEP OVERRIDES
DATASET_NAME="cl-5m_bk_set_10"
METRICS="logloss,average_precision"
TARGET_LONG="TARGET_TRIPLE_2x1_24H_LONG"
TARGET_SHORT="TARGET_TRIPLE_2x1_24H_SHORT"
CUTOFF="2022-01-01"
N_TRIALS=300
N_WORKERS=4
THREADS_PER_WORKER=12
DB_DIR="models/optuna_studies"
BUCKET="gs://cltrainer-optuna-results"
JOB_NAME="sweep"
STRATEGY="configs/strategies/ensemble4.json"
LOG="sweep_run_$(date +%Y%m%d_%H%M%S).log"
SHUTDOWN=false
USE_BUCKETS=false
AGENT_ID="${AGENT_ID:-sweep_bot}"

# Search space constraints (production sweep)
MAX_DEPTH_MIN=3
MAX_DEPTH_MAX=10
NUM_LEAVES_MIN=15
NUM_LEAVES_MAX=100
MAX_N_ESTIMATORS=2000
EARLY_STOPPING=25
MAX_FOLDS=5

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
    esac
done

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
    gsutil cp "$LOG" "$BUCKET/$JOB_NAME/logs/" 2>/dev/null || true
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
echo "    max_depth:       [$MAX_DEPTH_MIN, $MAX_DEPTH_MAX]" | tee -a "$LOG"
echo "    num_leaves:      [$NUM_LEAVES_MIN, $NUM_LEAVES_MAX]" | tee -a "$LOG"
echo "    n_estimators:    max $MAX_N_ESTIMATORS" | tee -a "$LOG"
echo "    early_stopping:  $EARLY_STOPPING rounds" | tee -a "$LOG"
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
gsutil cp "$LOG" "$BUCKET/$CANARY_PREFIX/logs/" 2>/dev/null || true

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
    fi
else
    echo "  ⚠ Skipping E2E pipeline — no searches completed." | tee -a "$LOG"
fi

# Upload final log
gsutil cp "$LOG" "$BUCKET/$CANARY_PREFIX/logs/" 2>/dev/null || true

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

# Shutdown VM if requested
if [ "$SHUTDOWN" = true ]; then
    echo "Shutting down VM in 30 seconds..." | tee -a "$LOG"
    gsutil cp "$LOG" "$BUCKET/$JOB_NAME/logs/" 2>/dev/null || true
    sleep 30
    sudo shutdown -h now
fi
