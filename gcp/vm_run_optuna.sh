#!/bin/bash
# =============================================================================
# VM E2E Alpha Factory Runner — executes inside a tmux session on the GCP VM
#
# This script:
#   1. Activates the Python venv
#   2. Runs the Optuna search for each metric × direction
#   3. Runs the E2E pipeline (train + backtest + package)
#   4. Uploads production_artifacts.zip to GCS
#   5. Optionally shuts down the VM
#
# Usage (called by gcp_deploy_run.ps1, or manually):
#   bash vm_run_optuna.sh \
#     --data /path/to/data.parquet \
#     --target TARGET_TRIPLE_2x1_24H_LONG \
#     --train-cutoff-date 2022-01-01 \
#     --n-trials 200 \
#     --n-jobs 12 \
#     --strategy-config configs/strategies/ensemble4.json \
#     [--e2e]  [--shutdown]
# =============================================================================

set -eo pipefail

# Activate environment
source /opt/optuna-env/bin/activate

PROJECT_DIR=~/project
RESULTS_DIR="${PROJECT_DIR}/models/optuna_studies"
BUCKET="gs://cltrainer-optuna-results"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
LOG_FILE="${PROJECT_DIR}/optuna_run_${TIMESTAMP}.log"

cd "$PROJECT_DIR"
mkdir -p "$RESULTS_DIR" reports

# ---- Parse custom flags ----
RUN_E2E=false
SHUTDOWN=false
STRATEGY_CONFIG=""
STUDY_NAME=""
OPTUNA_ARGS=()
DATA_PATH=""
CUTOFF_DATE=""

for arg in "$@"; do
    case "$arg" in
        --e2e)       RUN_E2E=true ;;
        --shutdown)  SHUTDOWN=true ;;
        *)           OPTUNA_ARGS+=("$arg") ;;
    esac
done

# Extract key args for E2E pipeline
for i in "${!OPTUNA_ARGS[@]}"; do
    case "${OPTUNA_ARGS[$i]}" in
        --strategy-config) STRATEGY_CONFIG="${OPTUNA_ARGS[$((i+1))]}" ;;
        --data)            DATA_PATH="${OPTUNA_ARGS[$((i+1))]}" ;;
        --train-cutoff-date) CUTOFF_DATE="${OPTUNA_ARGS[$((i+1))]}" ;;
        --study-name)      STUDY_NAME="${OPTUNA_ARGS[$((i+1))]}" ;;
    esac
done

echo "============================================================"
echo " E2E ALPHA FACTORY — GCP VM RUNNER"
echo "============================================================"
echo "  Timestamp:  $TIMESTAMP"
echo "  vCPUs:      $(nproc)"
echo "  RAM:        $(free -h | awk '/Mem:/ {print $2}')"
echo "  E2E Mode:   $RUN_E2E"
echo "  Shutdown:   $SHUTDOWN"
echo "  Strategy:   $STRATEGY_CONFIG"
echo "  Data:       $DATA_PATH"
echo "  Cutoff:     $CUTOFF_DATE"
echo "  Log:        $LOG_FILE"
echo "============================================================"
echo ""

# ---- Run Optuna search (tee to log + console) ----
echo "[PHASE 1] Running Optuna hyperparameter search..."
python agent/optuna_lgbm_search_v2.py "${OPTUNA_ARGS[@]}" 2>&1 | tee "$LOG_FILE" || true
OPTUNA_EXIT=${PIPESTATUS[0]}

echo ""
echo "============================================================"
echo " OPTUNA SEARCH COMPLETE (exit code: $OPTUNA_EXIT)"
echo "============================================================"

# ---- Upload Optuna results to GCS ----
echo ""
echo "Uploading Optuna results to $BUCKET..."

if ls ${RESULTS_DIR}/*.db 1>/dev/null 2>&1; then
    gsutil -m cp ${RESULTS_DIR}/*.db "$BUCKET/studies/"
    echo "  ✓ Uploaded .db study files"
fi

if ls reports/optuna_*.json 1>/dev/null 2>&1; then
    gsutil -m cp reports/optuna_*.json "$BUCKET/reports/"
    echo "  ✓ Uploaded report JSONs"
fi

if ls reports/optuna_*.csv 1>/dev/null 2>&1; then
    gsutil -m cp reports/optuna_*.csv "$BUCKET/reports/"
    echo "  ✓ Uploaded trial CSVs"
fi

gsutil cp "$LOG_FILE" "$BUCKET/logs/"
echo "  ✓ Uploaded run log"

# ---- Run E2E Pipeline (if enabled) ----
if [ "$RUN_E2E" = true ] && [ $OPTUNA_EXIT -eq 0 ]; then
    echo ""
    echo "============================================================"
    echo " [PHASE 2] Running E2E Pipeline (train + backtest + package)"
    echo "============================================================"
    echo ""

    E2E_ARGS=(
        --data "$DATA_PATH"
        --train-cutoff-date "$CUTOFF_DATE"
        --db-dir "$RESULTS_DIR"
        --output-dir "${PROJECT_DIR}/production_output"
        --gcs-bucket "$BUCKET"
    )

    if [ -n "$STRATEGY_CONFIG" ]; then
        E2E_ARGS+=(--strategy-config "$STRATEGY_CONFIG")
    fi

    if [ -n "$STUDY_NAME" ]; then
        E2E_ARGS+=(--study-prefix "$STUDY_NAME")
    fi

    if [ "$SHUTDOWN" = true ]; then
        E2E_ARGS+=(--shutdown)
    fi

    python gcp/vm_e2e_pipeline.py "${E2E_ARGS[@]}" 2>&1 | tee -a "$LOG_FILE" || true
    E2E_EXIT=${PIPESTATUS[0]}

    # Fallback shutdown if E2E crashed but --shutdown was requested
    if [ "$SHUTDOWN" = true ] && [ $E2E_EXIT -ne 0 ]; then
        echo "  ⚠ E2E pipeline failed (exit $E2E_EXIT) but --shutdown requested."
        echo "  Artifacts may be incomplete. Shutting down anyway..."
        sudo shutdown -h now
    fi

elif [ "$RUN_E2E" = true ] && [ $OPTUNA_EXIT -ne 0 ]; then
    echo ""
    echo "  ⚠ Skipping E2E pipeline — Optuna exited with error code $OPTUNA_EXIT"
fi

# ---- Shutdown if requested (and E2E not handling it) ----
if [ "$SHUTDOWN" = true ] && [ "$RUN_E2E" = false ]; then
    echo "Shutting down VM..."
    sudo shutdown -h now
fi

echo ""
echo "============================================================"
echo " ALL DONE"
echo "============================================================"
echo ""
echo "GCS bucket: $BUCKET"
echo ""
echo "Download results locally:"
echo "  gsutil -m cp -r ${BUCKET}/production/* ."
echo ""
echo "Or use:  .\gcp_teardown.ps1  (downloads + deletes VM)"
echo ""
echo "============================================================"
