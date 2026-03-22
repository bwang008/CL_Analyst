#!/bin/bash
# =============================================================================
# E2E Alpha Factory — Canary (Light) Run
#
# A stripped-down version of vm_production_run.sh for fast-feedback (~30 min).
#   - 4 searches (2 metrics × 2 directions) instead of 6
#   - 20 trials per search (not 200)
#   - Constrained search space (shallow trees, max 500 estimators)
#   - Output to gs://cltrainer-optuna-results/canary/ (isolated from production)
#   - Still runs full E2E pipeline (train + backtest + package) at end
#
# Usage (called by gcp_deploy_canary.ps1 or manually):
#   bash gcp/vm_canary_run.sh [--shutdown]
# =============================================================================

set -eo pipefail

# Activate environment
source /opt/optuna-env/bin/activate

PROJECT_DIR="/home/$(whoami)/project"
cd "$PROJECT_DIR"

# Configuration — CANARY OVERRIDES
DATA="/home/$(whoami)/data/cl-5m_bk_set_08.parquet"
CUTOFF="2022-01-01"
N_TRIALS=20
N_JOBS=4
NUM_THREADS=12
DB_DIR="models/optuna_studies"
BUCKET="gs://cltrainer-optuna-results"
CANARY_PREFIX="canary"
STRATEGY="configs/strategies/ensemble4.json"
LOG="canary_run_$(date +%Y%m%d_%H%M%S).log"
SHUTDOWN=false
AGENT_ID="${AGENT_ID:-canary_bot}"

# Search space constraints (fast canary)
MAX_DEPTH_MIN=3
MAX_DEPTH_MAX=5
NUM_LEAVES_MIN=15
NUM_LEAVES_MAX=31
MAX_N_ESTIMATORS=500
EARLY_STOPPING=20
MAX_FOLDS=5

# Parse args
for arg in "$@"; do
    case "$arg" in
        --shutdown) SHUTDOWN=true ;;
        --agent-id=*) AGENT_ID="${arg#*=}" ;;
    esac
done

# ---------- CPU VALIDATION ----------
SYSTEM_CPUS=$(nproc)
REQUIRED_CPUS=$((N_JOBS * NUM_THREADS))
if [ "$REQUIRED_CPUS" -ne "$SYSTEM_CPUS" ]; then
    echo "" | tee "$LOG"
    echo "FATAL: CPU mismatch!" | tee -a "$LOG"
    echo "  System CPUs:   $SYSTEM_CPUS" | tee -a "$LOG"
    echo "  Required:      $REQUIRED_CPUS (N_JOBS=$N_JOBS × NUM_THREADS=$NUM_THREADS)" | tee -a "$LOG"
    echo "  Fix: adjust N_JOBS and NUM_THREADS in this script to match the machine." | tee -a "$LOG"
    echo "" | tee -a "$LOG"
    gsutil cp "$LOG" "$BUCKET/$CANARY_PREFIX/logs/" 2>/dev/null || true
    exit 1
fi

# Only run logloss and f0.5 (skip average_precision)
COMBOS=(
    "TARGET_TRIPLE_2x1_24H_LONG logloss"
    "TARGET_TRIPLE_2x1_24H_SHORT logloss"
)

TOTAL=${#COMBOS[@]}
COMPLETED=0
FAILED=0
START_TIME=$(date +%s)

echo "============================================================" | tee "$LOG"
echo " E2E ALPHA FACTORY — CANARY (LIGHT) RUN" | tee -a "$LOG"
echo "============================================================" | tee -a "$LOG"
echo "  Timestamp:  $(date -Iseconds)" | tee -a "$LOG"
echo "  Agent:      $AGENT_ID" | tee -a "$LOG"
echo "  Hostname:   $(hostname)" | tee -a "$LOG"
echo "  CPUs:       $SYSTEM_CPUS (verified: $N_JOBS workers × $NUM_THREADS threads)" | tee -a "$LOG"
echo "  Data:       $DATA" | tee -a "$LOG"
echo "  Cutoff:     $CUTOFF" | tee -a "$LOG"
echo "  Trials:     $N_TRIALS per search (CANARY)" | tee -a "$LOG"
echo "  Workers:    $N_JOBS (× $NUM_THREADS LGB threads = $((N_JOBS * NUM_THREADS)) cores)" | tee -a "$LOG"
echo "  Searches:   $TOTAL (2 metrics × 2 directions)" | tee -a "$LOG"
echo "  Strategy:   $STRATEGY" | tee -a "$LOG"
echo "  Shutdown:   $SHUTDOWN" | tee -a "$LOG"
echo "  Log:        $LOG" | tee -a "$LOG"
echo "  GCS dest:   $BUCKET/$CANARY_PREFIX/" | tee -a "$LOG"
echo "" | tee -a "$LOG"
echo "  SEARCH SPACE CONSTRAINTS:" | tee -a "$LOG"
echo "    max_depth:       [$MAX_DEPTH_MIN, $MAX_DEPTH_MAX]" | tee -a "$LOG"
echo "    num_leaves:      [$NUM_LEAVES_MIN, $NUM_LEAVES_MAX]" | tee -a "$LOG"
echo "    n_estimators:    max $MAX_N_ESTIMATORS" | tee -a "$LOG"
echo "    early_stopping:  $EARLY_STOPPING rounds" | tee -a "$LOG"
echo "============================================================" | tee -a "$LOG"
echo "" | tee -a "$LOG"

# =============================================================================
# PHASE 1: Run Optuna searches (constrained)
# =============================================================================

for i in "${!COMBOS[@]}"; do
    combo="${COMBOS[$i]}"
    read -r TARGET METRIC <<< "$combo"

    # Extract direction from target name
    if [[ "$TARGET" == *"LONG" ]]; then
        DIR="long"
    else
        DIR="short"
    fi
    STUDY="${CANARY_PREFIX}_${DIR}_${METRIC}"
    SEARCH_NUM=$((i + 1))

    echo "" | tee -a "$LOG"
    echo "============================================================" | tee -a "$LOG"
    echo " [$(date -Iseconds)] SEARCH ${SEARCH_NUM}/${TOTAL}: ${DIR^^} ${METRIC} (CANARY)" | tee -a "$LOG"
    echo " Study: $STUDY" | tee -a "$LOG"
    echo "============================================================" | tee -a "$LOG"
    echo "" | tee -a "$LOG"

    # Clean up any stale journal file from previous runs to avoid resuming RUNNING trials
    if [ -f "${DB_DIR}/${STUDY}.journal" ]; then
        echo "  Removing stale journal: ${DB_DIR}/${STUDY}.journal" | tee -a "$LOG"
        rm -f "${DB_DIR}/${STUDY}.journal"
    fi

    # Run search — capture exit code properly (|| true masks PIPESTATUS, so use set +e)
    set +e
    python agent/optuna_lgbm_search_v2.py \
        --target "$TARGET" \
        --data "$DATA" \
        --ml-metric "$METRIC" \
        --n-trials "$N_TRIALS" \
        --n-jobs "$N_JOBS" \
        --study-name "$STUDY" \
        --db-dir "$DB_DIR" \
        --train-cutoff-date "$CUTOFF" \
        --max-depth-range $MAX_DEPTH_MIN $MAX_DEPTH_MAX \
        --num-leaves-range $NUM_LEAVES_MIN $NUM_LEAVES_MAX \
        --max-n-estimators $MAX_N_ESTIMATORS \
        --early-stopping-rounds $EARLY_STOPPING \
        --num-threads $NUM_THREADS \
        --max-folds $MAX_FOLDS \
        2>&1 | tee -a "$LOG"

    SEARCH_EXIT=${PIPESTATUS[0]}
    set -e

    if [ $SEARCH_EXIT -eq 0 ]; then
        COMPLETED=$((COMPLETED + 1))
        echo "  ✓ Search ${SEARCH_NUM}/${TOTAL} PASSED (${DIR} ${METRIC})" | tee -a "$LOG"
    else
        FAILED=$((FAILED + 1))
        echo "  ✗ Search ${SEARCH_NUM}/${TOTAL} FAILED (exit $SEARCH_EXIT)" | tee -a "$LOG"
    fi

    # Upload intermediate results to canary GCS folder
    gsutil -m cp ${DB_DIR}/${STUDY}.journal "$BUCKET/$CANARY_PREFIX/studies/" 2>/dev/null || true
    gsutil -m cp reports/optuna_*_${DIR}_${METRIC}.* "$BUCKET/$CANARY_PREFIX/reports/" 2>/dev/null || true
    echo "  Uploaded ${STUDY} results to GCS ($CANARY_PREFIX/)" | tee -a "$LOG"

    # Upload STATUS.json for monitor polling
    echo "{\"completed\": $COMPLETED, \"failed\": $FAILED, \"total\": $TOTAL, \"current\": \"${DIR}_${METRIC}\", \"agent\": \"$AGENT_ID\", \"last_update\": \"$(date -Iseconds)\"}" | \
        gsutil cp - "$BUCKET/$CANARY_PREFIX/STATUS.json" 2>/dev/null || true
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
        --gcs-prefix "$CANARY_PREFIX"
        --metrics logloss f0.5
        --study-prefix "$CANARY_PREFIX"
    )

    python gcp/vm_e2e_pipeline.py "${E2E_ARGS[@]}" 2>&1 | tee -a "$LOG" || true
    E2E_EXIT=${PIPESTATUS[0]}

    if [ $E2E_EXIT -eq 0 ]; then
        echo "  ✓ E2E pipeline completed successfully" | tee -a "$LOG"
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
echo "  GCS results:     $BUCKET/$CANARY_PREFIX/" | tee -a "$LOG"
echo "============================================================" | tee -a "$LOG"
echo "" | tee -a "$LOG"
echo "Download results:" | tee -a "$LOG"
echo "  gsutil -m cp -r ${BUCKET}/${CANARY_PREFIX}/* ." | tee -a "$LOG"
echo "" | tee -a "$LOG"

# Shutdown VM if requested
if [ "$SHUTDOWN" = true ]; then
    echo "Shutting down canary VM in 30 seconds..." | tee -a "$LOG"
    gsutil cp "$LOG" "$BUCKET/$CANARY_PREFIX/logs/" 2>/dev/null || true
    sleep 30
    sudo shutdown -h now
fi
