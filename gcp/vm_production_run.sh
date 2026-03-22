#!/bin/bash
# =============================================================================
# E2E Alpha Factory — Full Production Run (Preemption-Safe)
#
# Runs ALL 6 Optuna searches (3 metrics × 2 directions) in sequence,
# then the E2E pipeline once at the end to process all results.
#
# PREEMPTION SAFE:
#   - Checks existing trial count in .db and only runs remaining trials
#   - Skips completed searches (already at N_TRIALS)
#   - Uploads .db to GCS after each search completion
#   - Auto-launches via VM startup metadata on reboot
#
# Usage:
#   bash gcp/vm_production_run.sh [--shutdown]
# =============================================================================

set -eo pipefail

# Activate environment
source /opt/optuna-env/bin/activate

PROJECT_DIR="/home/$(whoami)/project"
cd "$PROJECT_DIR"

# Configuration
DATASET_NAME="cl-5m_bk_set_10.parquet"
DATA="/home/$(whoami)/data/${DATASET_NAME}"
GCS_DATA="gs://cltrainer-optuna-results/data/${DATASET_NAME}"
CUTOFF="2022-01-01"
N_TRIALS=100
N_JOBS=4
NUM_THREADS=12
DB_DIR="models/optuna_studies"
BUCKET="gs://cltrainer-optuna-results"
STRATEGY="configs/strategies/ensemble4.json"
LOG="optuna_production_$(date +%Y%m%d_%H%M%S).log"
SHUTDOWN=false
AGENT_ID="${AGENT_ID:-production_bot}"

# ---------- GCS Data Staging ----------
# Pull dataset from GCS if not already on disk (fast: ~30s within GCP)
if [ ! -f "$DATA" ]; then
    echo "Downloading dataset from GCS: $GCS_DATA"
    mkdir -p "$(dirname "$DATA")"
    gsutil cp "$GCS_DATA" "$DATA"
    echo "  Downloaded: $(du -h "$DATA" | cut -f1)"
else
    echo "Dataset already on disk: $(du -h "$DATA" | cut -f1)"
fi

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
    gsutil cp "$LOG" "$BUCKET/logs/" 2>/dev/null || true
    exit 1
fi

# -------------------------------------------------------------------
# Helper: count existing trials in a study .db file
# Returns the number of completed trials, or 0 if no .db exists
# -------------------------------------------------------------------
count_existing_trials() {
    local db_path="$1"
    local study_name="$2"
    if [ ! -f "$db_path" ]; then
        echo 0
        return
    fi
    python -c "
import optuna
optuna.logging.set_verbosity(optuna.logging.WARNING)
try:
    s = optuna.load_study(study_name='${study_name}', storage='sqlite:///${db_path}')
    print(len([t for t in s.trials if t.state == optuna.trial.TrialState.COMPLETE]))
except:
    print(0)
"
}

# Define all 6 search combos
COMBOS=(
    "TARGET_TRIPLE_2x1_24H_LONG logloss"
    "TARGET_TRIPLE_2x1_24H_LONG f0.5"
    "TARGET_TRIPLE_2x1_24H_LONG average_precision"
    "TARGET_TRIPLE_2x1_24H_SHORT logloss"
    "TARGET_TRIPLE_2x1_24H_SHORT f0.5"
    "TARGET_TRIPLE_2x1_24H_SHORT average_precision"
)

TOTAL=${#COMBOS[@]}
COMPLETED=0
SKIPPED=0
FAILED=0
START_TIME=$(date +%s)

echo "============================================================" | tee "$LOG"
echo " E2E ALPHA FACTORY — PRODUCTION RUN (Preemption-Safe)" | tee -a "$LOG"
echo "============================================================" | tee -a "$LOG"
echo "  Timestamp:  $(date -Iseconds)" | tee -a "$LOG"
echo "  Agent:      $AGENT_ID" | tee -a "$LOG"
echo "  Hostname:   $(hostname)" | tee -a "$LOG"
echo "  CPUs:       $SYSTEM_CPUS (verified: $N_JOBS workers × $NUM_THREADS threads)" | tee -a "$LOG"
echo "  Data:       $DATA" | tee -a "$LOG"
echo "  Cutoff:     $CUTOFF" | tee -a "$LOG"
echo "  Trials:     $N_TRIALS per search (resumes from existing)" | tee -a "$LOG"
echo "  Workers:    $N_JOBS (× $NUM_THREADS LGB threads = $((N_JOBS * NUM_THREADS)) cores)" | tee -a "$LOG"
echo "  Searches:   $TOTAL (3 metrics × 2 directions)" | tee -a "$LOG"
echo "  Strategy:   $STRATEGY" | tee -a "$LOG"
echo "  Shutdown:   $SHUTDOWN" | tee -a "$LOG"
echo "  Log:        $LOG" | tee -a "$LOG"
echo "============================================================" | tee -a "$LOG"
echo "" | tee -a "$LOG"

# =============================================================================
# PHASE 1: Run all 6 Optuna searches (with smart resume)
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
    STUDY="e2e_${DIR}_${METRIC}"
    SEARCH_NUM=$((i + 1))
    DB_PATH="${DB_DIR}/${STUDY}.db"

    # Check existing trial count
    EXISTING=$(count_existing_trials "$DB_PATH" "$STUDY")
    REMAINING=$((N_TRIALS - EXISTING))

    echo "" | tee -a "$LOG"
    echo "============================================================" | tee -a "$LOG"
    echo " [$(date -Iseconds)] SEARCH ${SEARCH_NUM}/${TOTAL}: ${DIR^^} ${METRIC}" | tee -a "$LOG"
    echo " Study: $STUDY" | tee -a "$LOG"
    echo " Existing trials: ${EXISTING}/${N_TRIALS}" | tee -a "$LOG"

    # Skip if already complete
    if [ "$REMAINING" -le 0 ]; then
        echo " ✓ ALREADY COMPLETE — skipping" | tee -a "$LOG"
        echo "============================================================" | tee -a "$LOG"
        COMPLETED=$((COMPLETED + 1))
        SKIPPED=$((SKIPPED + 1))
        continue
    fi

    echo " Remaining trials: $REMAINING" | tee -a "$LOG"
    echo "============================================================" | tee -a "$LOG"
    echo "" | tee -a "$LOG"

    python agent/optuna_lgbm_search_v2.py \
        --target "$TARGET" \
        --data "$DATA" \
        --ml-metric "$METRIC" \
        --n-trials "$REMAINING" \
        --n-jobs "$N_JOBS" \
        --study-name "$STUDY" \
        --db-dir "$DB_DIR" \
        --train-cutoff-date "$CUTOFF" \
        --max-n-estimators 2000 \
        --num-threads $NUM_THREADS \
        2>&1 | tee -a "$LOG" || true

    SEARCH_EXIT=${PIPESTATUS[0]}

    if [ $SEARCH_EXIT -eq 0 ]; then
        COMPLETED=$((COMPLETED + 1))
        echo "  ✓ Search ${SEARCH_NUM}/${TOTAL} PASSED (${DIR} ${METRIC})" | tee -a "$LOG"
    else
        FAILED=$((FAILED + 1))
        echo "  ✗ Search ${SEARCH_NUM}/${TOTAL} FAILED (exit $SEARCH_EXIT)" | tee -a "$LOG"
    fi

    # Upload intermediate results to GCS after each search
    gsutil -m cp ${DB_PATH} "$BUCKET/studies/" 2>/dev/null || true
    gsutil -m cp reports/optuna_*_${DIR}_${METRIC}.* "$BUCKET/reports/" 2>/dev/null || true
    gsutil cp "$LOG" "$BUCKET/logs/" 2>/dev/null || true
    echo "  Uploaded ${STUDY}.db to GCS" | tee -a "$LOG"

    # Upload STATUS.json for monitor polling
    echo "{\"completed\": $COMPLETED, \"failed\": $FAILED, \"total\": $TOTAL, \"current\": \"${DIR}_${METRIC}\", \"agent\": \"$AGENT_ID\", \"skipped\": $SKIPPED, \"last_update\": \"$(date -Iseconds)\"}" | \
        gsutil cp - "$BUCKET/STATUS.json" 2>/dev/null || true
done

SEARCH_ELAPSED=$(( $(date +%s) - START_TIME ))
echo "" | tee -a "$LOG"
echo "============================================================" | tee -a "$LOG"
echo " ALL SEARCHES COMPLETE" | tee -a "$LOG"
echo "  Completed: ${COMPLETED}/${TOTAL} (${SKIPPED} skipped/resumed)" | tee -a "$LOG"
echo "  Failed:    ${FAILED}/${TOTAL}" | tee -a "$LOG"
echo "  Elapsed:   $((SEARCH_ELAPSED / 3600))h $((SEARCH_ELAPSED % 3600 / 60))m" | tee -a "$LOG"
echo "============================================================" | tee -a "$LOG"

# =============================================================================
# PHASE 2: Run E2E pipeline (processes all .db files)
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
        --output-dir "${PROJECT_DIR}/production_output"
        --gcs-bucket "$BUCKET"
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
gsutil cp "$LOG" "$BUCKET/logs/" 2>/dev/null || true

TOTAL_ELAPSED=$(( $(date +%s) - START_TIME ))
echo "" | tee -a "$LOG"
echo "============================================================" | tee -a "$LOG"
echo " PRODUCTION RUN COMPLETE" | tee -a "$LOG"
echo "  Total wall time: $((TOTAL_ELAPSED / 3600))h $((TOTAL_ELAPSED % 3600 / 60))m" | tee -a "$LOG"
echo "  Searches:        ${COMPLETED}/${TOTAL} passed" | tee -a "$LOG"
echo "  GCS bucket:      $BUCKET" | tee -a "$LOG"
echo "============================================================" | tee -a "$LOG"
echo "" | tee -a "$LOG"

# Shutdown VM if requested
if [ "$SHUTDOWN" = true ]; then
    echo "Shutting down VM in 30 seconds..." | tee -a "$LOG"
    gsutil cp "$LOG" "$BUCKET/logs/" 2>/dev/null || true
    sleep 30
    sudo shutdown -h now
fi
