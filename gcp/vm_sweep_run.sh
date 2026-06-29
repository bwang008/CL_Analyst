#!/bin/bash
# =============================================================================
# TRANSPORT WRAPPER
# Runs the Python E2E orchestrator, then uploads the run log to the job-scoped
# GCS prefix (gs://bucket/<job>/logs/) and optionally self-terminates the VM so
# the batch monitor (gcp_monitor.ps1) can detect completion.
#
#   --gcs-prefix=<job>   Job-scoped GCS prefix (forwarded to orchestrator too)
#   --shutdown           Self-terminate the VM after logs are uploaded
#   (all other args are forwarded verbatim to gcp/orchestrator.py)
# =============================================================================

LOG="sweep_run_$(date +%Y%m%d_%H%M%S).log"
BUCKET="gs://cltrainer-optuna-results"

# --- Parse operational flags. --shutdown is consumed here (NOT forwarded to the
#     orchestrator, which would otherwise pass it to the search workers).
#     --gcs-prefix is captured AND forwarded so Phase 2 uploads to the prefix. ---
GCS_PREFIX=""
SHUTDOWN=false
ORCH_ARGS=()
for arg in "$@"; do
    case "$arg" in
        --gcs-prefix=*) GCS_PREFIX="${arg#*=}"; ORCH_ARGS+=("$arg") ;;
        --shutdown)     SHUTDOWN=true ;;
        *)              ORCH_ARGS+=("$arg") ;;
    esac
done

finish() {
    local code=$1
    # Upload the run log so the monitor / artifact gate can read it.
    if [ -n "$GCS_PREFIX" ]; then
        gsutil cp "$LOG" "$BUCKET/$GCS_PREFIX/logs/$(hostname)_$LOG" 2>/dev/null || true
    else
        local sub="orchestrator_failures"
        [ "$code" -eq 0 ] && sub="orchestrator_success"
        gsutil cp "$LOG" "$BUCKET/logs/$sub/$(hostname)_$LOG" 2>/dev/null || true
    fi
    # Self-terminate AFTER the log (with the completion marker) is uploaded so the
    # monitor sees TERMINATED and finds the "E2E PIPELINE COMPLETE" marker.
    if [ "$SHUTDOWN" = true ]; then
        echo "Self-terminating VM..." | tee -a "$LOG"
        sudo shutdown -h now
    fi
}

# Activate environment
source /opt/optuna-env/bin/activate
export PYTHONPATH="$(pwd):$PYTHONPATH"

echo "============================================================" | tee -a "$LOG"
echo " Launching E2E Python Orchestrator..." | tee -a "$LOG"
echo "============================================================" | tee -a "$LOG"

# Run orchestrator; capture ITS exit code (not tee's) via PIPESTATUS.
python3 gcp/orchestrator.py "${ORCH_ARGS[@]}" 2>&1 | tee -a "$LOG"
EXIT_CODE=${PIPESTATUS[0]}

if [ "$EXIT_CODE" -ne 0 ]; then
    echo "FATAL: Python Orchestrator failed with exit code $EXIT_CODE" | tee -a "$LOG"
    finish "$EXIT_CODE"
    exit "$EXIT_CODE"
fi

echo "============================================================" | tee -a "$LOG"
echo " Orchestrator Completed Successfully." | tee -a "$LOG"
echo "============================================================" | tee -a "$LOG"
finish 0
