#!/bin/bash
# =============================================================================
# VM Optuna Runner — executes inside a tmux session on the GCP VM
#
# This script:
#   1. Activates the Python venv
#   2. Runs the Optuna search with provided arguments
#   3. Uploads results (.db, JSON, CSV) to GCS bucket
#   4. Logs all output to a timestamped file
#
# Usage (called by gcp_deploy_run.ps1, not directly):
#   bash vm_run_optuna.sh --target TARGET_TRIPLE_2x1_24H_LONG --data /path/to/data.parquet ...
# =============================================================================

set -e

# Activate environment
source /opt/optuna-env/bin/activate

PROJECT_DIR=~/project
RESULTS_DIR="${PROJECT_DIR}/models/optuna_studies"
BUCKET="gs://cltrainer-optuna-results"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
LOG_FILE="${PROJECT_DIR}/optuna_run_${TIMESTAMP}.log"

cd "$PROJECT_DIR"
mkdir -p "$RESULTS_DIR" reports

echo "============================================================"
echo " OPTUNA SEARCH — GCP VM"
echo "============================================================"
echo "  Timestamp: $TIMESTAMP"
echo "  Args:      $@"
echo "  Log:       $LOG_FILE"
echo "  vCPUs:     $(nproc)"
echo "  RAM:       $(free -h | awk '/Mem:/ {print $2}')"
echo "============================================================"
echo ""

# ---- Run Optuna (tee to log + console) ----
python agent/optuna_lgbm_search_v2.py "$@" 2>&1 | tee "$LOG_FILE"
EXIT_CODE=${PIPESTATUS[0]}

echo ""
echo "============================================================"
echo " OPTUNA COMPLETE (exit code: $EXIT_CODE)"
echo "============================================================"

# ---- Upload results to GCS ----
echo ""
echo "Uploading results to $BUCKET..."

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

echo ""
echo "============================================================"
echo " RESULTS AVAILABLE"
echo "============================================================"
echo ""
echo "GCS bucket: $BUCKET"
echo ""
echo "Download all results locally:"
echo "  gsutil -m cp -r ${BUCKET}/studies/* models/optuna_studies/"
echo "  gsutil -m cp -r ${BUCKET}/reports/* reports/"
echo ""
echo "Or use:  .\gcp_teardown.ps1  (downloads + deletes VM)"
echo ""
echo "============================================================"
