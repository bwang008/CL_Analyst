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
DATASET_NAME="cl-4h_bk_set_01.parquet"
DATA="/home/$(whoami)/data/${DATASET_NAME}"
GCS_DATA="gs://cltrainer-optuna-results/data/${DATASET_NAME}"
CUTOFF="2022-01-01"
N_TRIALS=2000
N_WORKERS=4
THREADS_PER_WORKER=12
DB_DIR="models/optuna_studies"
BUCKET="gs://cltrainer-optuna-results/production_4h_v2"
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
REQUIRED_CPUS=$((N_WORKERS * THREADS_PER_WORKER))
if [ "$REQUIRED_CPUS" -ne "$SYSTEM_CPUS" ]; then
    echo "" | tee "$LOG"
    echo "FATAL: CPU mismatch!" | tee -a "$LOG"
    echo "  System CPUs:   $SYSTEM_CPUS" | tee -a "$LOG"
    echo "  Required:      $REQUIRED_CPUS (N_WORKERS=$N_WORKERS × THREADS_PER_WORKER=$THREADS_PER_WORKER)" | tee -a "$LOG"
    echo "  Fix: adjust N_WORKERS and THREADS_PER_WORKER in this script to match the machine." | tee -a "$LOG"
    echo "" | tee -a "$LOG"
    gsutil cp "$LOG" "$BUCKET/logs/" 2>/dev/null || true
    exit 1
fi
TRIALS_PER_WORKER=$((N_TRIALS / N_WORKERS))

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
    "TARGET_TRIPLE_2x1_30B_LONG logloss"
    "TARGET_TRIPLE_2x1_30B_LONG average_precision"
    "TARGET_TRIPLE_2x1_30B_SHORT logloss"
    "TARGET_TRIPLE_2x1_30B_SHORT average_precision"
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
echo "  CPUs:       $SYSTEM_CPUS (verified: $N_WORKERS workers × $THREADS_PER_WORKER threads)" | tee -a "$LOG"
echo "  Data:       $DATA" | tee -a "$LOG"
echo "  Cutoff:     $CUTOFF" | tee -a "$LOG"
echo "  Trials:     $N_TRIALS per search ($TRIALS_PER_WORKER per worker × $N_WORKERS workers, resumes from existing)" | tee -a "$LOG"
echo "  Workers:    $N_WORKERS OS processes (× $THREADS_PER_WORKER LGB threads = $((N_WORKERS * THREADS_PER_WORKER)) cores)" | tee -a "$LOG"
echo "  Searches:   $TOTAL (3 metrics × 2 directions)" | tee -a "$LOG"
echo "  Strategy:   $STRATEGY" | tee -a "$LOG"
echo "  Shutdown:   $SHUTDOWN" | tee -a "$LOG"
echo "  Log:        $LOG" | tee -a "$LOG"
echo "============================================================" | tee -a "$LOG"
echo "" | tee -a "$LOG"

# =============================================================================
# PHASE 1: Run all 6 Optuna searches (with smart resume)
# =============================================================================

cat << 'EOF' > /tmp/update_status.py
import json, glob, datetime
try:
    with open('/tmp/base_status.json') as f: out = json.load(f)
except:
    out = {}
workers = []
for f in glob.glob('/tmp/worker_W*_status.json'):
    try:
        workers.append(json.load(open(f)))
    except: pass
out['workers'] = workers
out['last_update'] = datetime.datetime.now().isoformat()
with open('/tmp/STATUS.json', 'w') as f: json.dump(out, f)
EOF

echo "{\"completed\": 0, \"failed\": 0, \"total\": $TOTAL, \"current\": \"starting\", \"agent\": \"$AGENT_ID\", \"skipped\": 0}" > /tmp/base_status.json
python /tmp/update_status.py
gsutil cp /tmp/STATUS.json "$BUCKET/STATUS.json" 2>/dev/null || true

# Start background updater
(
while true; do
    sleep 60
    python /tmp/update_status.py
    gsutil cp /tmp/STATUS.json "$BUCKET/STATUS.json" 2>/dev/null || true
done
) &
UPDATER_PID=$!

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

    # Clean up stale worker status files
    rm -f /tmp/worker_W*_status.json 2>/dev/null || true

    # Distribute REMAINING trials across workers
    REMAINING_PER_WORKER=$((REMAINING / N_WORKERS))
    REMAINING_EXTRA=$((REMAINING % N_WORKERS))

    # Launch N_WORKERS parallel OS processes, each running n_jobs=1
    WORKER_PIDS=()
    for WORKER_ID in $(seq 1 $N_WORKERS); do
        # Give extra trials to the first worker if not evenly divisible
        WORKER_TRIALS=$REMAINING_PER_WORKER
        if [ $WORKER_ID -le $REMAINING_EXTRA ]; then
            WORKER_TRIALS=$((WORKER_TRIALS + 1))
        fi
        [ $WORKER_TRIALS -le 0 ] && continue

        python agent/optuna_lgbm_search_v2.py \
            --target "$TARGET" \
            --data "$DATA" \
            --ml-metric "$METRIC" \
            --n-trials "$WORKER_TRIALS" \
            --n-jobs 1 \
            --study-name "$STUDY" \
            --db-dir "$DB_DIR" \
            --train-cutoff-date "$CUTOFF" \
            --num-threads $THREADS_PER_WORKER \
            --worker-id $WORKER_ID \
            --use-buckets \
            2>&1 | tee -a "$LOG" &
        WORKER_PIDS+=($!)
        echo "  Started worker W${WORKER_ID} (PID $!, $WORKER_TRIALS trials)" | tee -a "$LOG"
    done

    # Wait for all workers and capture exit codes
    WORKER_FAILURES=0
    for idx in "${!WORKER_PIDS[@]}"; do
        wait ${WORKER_PIDS[$idx]}
        EXIT_CODE=$?
        WID=$((idx + 1))
        if [ $EXIT_CODE -ne 0 ]; then
            echo "  Worker W${WID} FAILED (exit $EXIT_CODE)" | tee -a "$LOG"
            WORKER_FAILURES=$((WORKER_FAILURES + 1))
        else
            echo "  Worker W${WID} completed OK" | tee -a "$LOG"
        fi
    done

    if [ $WORKER_FAILURES -eq 0 ]; then
        COMPLETED=$((COMPLETED + 1))
        echo "  ✓ Search ${SEARCH_NUM}/${TOTAL} PASSED (${DIR} ${METRIC})" | tee -a "$LOG"
    else
        FAILED=$((FAILED + 1))
        echo "  ✗ Search ${SEARCH_NUM}/${TOTAL} FAILED ($WORKER_FAILURES/$N_WORKERS workers failed)" | tee -a "$LOG"
    fi

    # Upload intermediate results to GCS after each search
    gsutil -m cp ${DB_DIR}/${STUDY}.journal "$BUCKET/studies/" 2>/dev/null || true
    gsutil -m cp reports/optuna_*_${DIR}_${METRIC}.* "$BUCKET/reports/" 2>/dev/null || true
    gsutil cp "$LOG" "$BUCKET/logs/" 2>/dev/null || true
    echo "  Uploaded ${STUDY}.db to GCS" | tee -a "$LOG"

    # Upload STATUS.json for monitor polling
    echo "{\"completed\": $COMPLETED, \"failed\": $FAILED, \"total\": $TOTAL, \"current\": \"${DIR}_${METRIC}\", \"agent\": \"$AGENT_ID\", \"skipped\": $SKIPPED}" > /tmp/base_status.json
    python /tmp/update_status.py
    gsutil cp /tmp/STATUS.json "$BUCKET/STATUS.json" 2>/dev/null || true
done

SEARCH_ELAPSED=$(( $(date +%s) - START_TIME ))
kill $UPDATER_PID 2>/dev/null || true
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
