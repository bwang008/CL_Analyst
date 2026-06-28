#!/bin/bash
# =============================================================================
# DUMB TRANSPORT WRAPPER
# Forwards execution to Python Orchestrator
# =============================================================================

echo "============================================================"
echo " Launching E2E Python Orchestrator..."
echo "============================================================"

# Ensure gcp directory is in PYTHONPATH
export PYTHONPATH="$(pwd):$PYTHONPATH"

# Pass all arguments transparently to the Python orchestrator
python3 gcp/orchestrator.py "$@"

EXIT_CODE=$?
if [ $EXIT_CODE -ne 0 ]; then
    echo "FATAL: Python Orchestrator failed with exit code $EXIT_CODE"
    exit $EXIT_CODE
fi

echo "============================================================"
echo " Orchestrator Completed Successfully."
echo "============================================================"
