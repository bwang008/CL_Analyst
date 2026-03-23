#!/bin/bash
# =============================================================================
# OVERNIGHT THREE-PRONG ALPHA SEARCH
#
# Runs three fundamentally different alpha experiments sequentially:
#   Exp 1: Asymmetric Loss Function (5× FP penalty)
#   Exp 2: Triple Barrier Method Target (1.5×/1.0× ATR, 72H)
#   Exp 3: Volatility Expansion Prediction (top-20% TR)
#
# Each experiment is isolated in its own subshell — if one fails, the next
# still runs. All results are uploaded to GCS under overnight_alpha/ prefix.
#
# Usage:
#   bash gcp/vm_overnight_alpha.sh [--shutdown] [--dataset=<name>]
# =============================================================================

set -o pipefail  # Note: NOT set -e, so one experiment failing doesn't kill the script

# Activate environment
source /opt/optuna-env/bin/activate

PROJECT_DIR="/home/$(whoami)/project"
cd "$PROJECT_DIR"

# ---- Configuration ----
DATASET_NAME="${DATASET_NAME:-cl-5m_bk_set_11}"
TARGET_STANDARD="TARGET_TRIPLE_2x1_24H_LONG"
CUTOFF="2022-01-01"
N_TRIALS=30
N_WORKERS=4
THREADS_PER_WORKER=12
DB_DIR="models/optuna_studies"
BUCKET="gs://cltrainer-optuna-results"
GCS_PREFIX="overnight_alpha"
STRATEGY="configs/strategies/ensemble4.json"
LOG="overnight_alpha_$(date +%Y%m%d_%H%M%S).log"
SHUTDOWN=false

# Search space constraints (canary-level)
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
        --dataset=*) DATASET_NAME="${arg#*=}" ;;
        --n-trials=*) N_TRIALS="${arg#*=}" ;;
    esac
done

DATA="/home/$(whoami)/data/${DATASET_NAME}.parquet"

START_TIME=$(date +%s)
EXP1_STATUS="NOT_RUN"
EXP2_STATUS="NOT_RUN"
EXP3_STATUS="NOT_RUN"

echo "============================================================" | tee "$LOG"
echo " OVERNIGHT THREE-PRONG ALPHA SEARCH" | tee -a "$LOG"
echo "============================================================" | tee -a "$LOG"
echo "  Timestamp:  $(date -Iseconds)" | tee -a "$LOG"
echo "  Hostname:   $(hostname)" | tee -a "$LOG"
echo "  Data:       $DATA" | tee -a "$LOG"
echo "  Cutoff:     $CUTOFF" | tee -a "$LOG"
echo "  Trials:     $N_TRIALS per experiment" | tee -a "$LOG"
echo "  Shutdown:   $SHUTDOWN" | tee -a "$LOG"
echo "  GCS dest:   $BUCKET/$GCS_PREFIX/" | tee -a "$LOG"
echo "============================================================" | tee -a "$LOG"
echo "" | tee -a "$LOG"

# Upload initial STATUS.json
update_status() {
    echo "{\"exp1\": \"$EXP1_STATUS\", \"exp2\": \"$EXP2_STATUS\", \"exp3\": \"$EXP3_STATUS\", \"last_update\": \"$(date -Iseconds)\"}" | \
        gsutil cp - "$BUCKET/$GCS_PREFIX/STATUS.json" 2>/dev/null || true
}
update_status

# =============================================================================
# EXPERIMENT 1: Asymmetric Loss Function
# =============================================================================
echo "" | tee -a "$LOG"
echo "============================================================" | tee -a "$LOG"
echo " [EXP 1/3] ASYMMETRIC LOSS FUNCTION" | tee -a "$LOG"
echo "  Hypothesis: 5× FP penalty → higher precision naturally" | tee -a "$LOG"
echo "  Target: $TARGET_STANDARD (standard directional)" | tee -a "$LOG"
echo "  Objective: asymmetric" | tee -a "$LOG"
echo "============================================================" | tee -a "$LOG"
EXP1_STATUS="RUNNING"
update_status

EXP1_STUDY="${GCS_PREFIX}_exp1_asymmetric"
rm -f "${DB_DIR}/${EXP1_STUDY}.journal" 2>/dev/null

(
    python agent/optuna_lgbm_search_v2.py \
        --target "$TARGET_STANDARD" \
        --data "$DATA" \
        --ml-metric logloss \
        --objective asymmetric \
        --n-trials "$N_TRIALS" \
        --n-jobs 1 \
        --study-name "$EXP1_STUDY" \
        --db-dir "$DB_DIR" \
        --train-cutoff-date "$CUTOFF" \
        --max-depth-range $MAX_DEPTH_MIN $MAX_DEPTH_MAX \
        --num-leaves-range $NUM_LEAVES_MIN $NUM_LEAVES_MAX \
        --max-n-estimators $MAX_N_ESTIMATORS \
        --early-stopping-rounds $EARLY_STOPPING \
        --num-threads $THREADS_PER_WORKER \
        --max-folds $MAX_FOLDS
) 2>&1 | tee -a "$LOG"
EXP1_EXIT=${PIPESTATUS[0]}

if [ $EXP1_EXIT -eq 0 ]; then
    EXP1_STATUS="PASSED"
    echo "  ✓ EXP 1 PASSED" | tee -a "$LOG"
else
    EXP1_STATUS="FAILED (exit $EXP1_EXIT)"
    echo "  ✗ EXP 1 FAILED (exit $EXP1_EXIT)" | tee -a "$LOG"
fi
update_status

# Upload Exp 1 artifacts
gsutil -m cp ${DB_DIR}/${EXP1_STUDY}.journal "$BUCKET/$GCS_PREFIX/studies/" 2>/dev/null || true
gsutil -m cp reports/optuna_*_long_logloss.* "$BUCKET/$GCS_PREFIX/reports/" 2>/dev/null || true
gsutil cp "$LOG" "$BUCKET/$GCS_PREFIX/logs/" 2>/dev/null || true

# =============================================================================
# EXPERIMENT 2: Triple Barrier Method Target
# =============================================================================
echo "" | tee -a "$LOG"
echo "============================================================" | tee -a "$LOG"
echo " [EXP 2/3] TRIPLE BARRIER METHOD TARGET" | tee -a "$LOG"
echo "  Hypothesis: TBM labels handle early profit-taking better" | tee -a "$LOG"
echo "  Target: TARGET_TBM_LONG (1.5×ATR TP / 1.0×ATR SL / 72H)" | tee -a "$LOG"
echo "============================================================" | tee -a "$LOG"
EXP2_STATUS="RUNNING"
update_status

# Step 2a: Generate TBM target
TBM_DATA="/home/$(whoami)/data/${DATASET_NAME}_tbm.parquet"
echo "  Generating TBM target..." | tee -a "$LOG"
(
    python scripts/generate_tbm_target.py \
        --data "$DATA" \
        --output "$TBM_DATA" \
        --tp-atr 1.5 \
        --sl-atr 1.0 \
        --horizon 864
) 2>&1 | tee -a "$LOG"
TBM_GEN_EXIT=${PIPESTATUS[0]}

if [ $TBM_GEN_EXIT -ne 0 ]; then
    EXP2_STATUS="FAILED (target generation, exit $TBM_GEN_EXIT)"
    echo "  ✗ EXP 2 FAILED at target generation" | tee -a "$LOG"
else
    # Step 2b: Run Optuna search on TBM target
    EXP2_STUDY="${GCS_PREFIX}_exp2_tbm"
    rm -f "${DB_DIR}/${EXP2_STUDY}.journal" 2>/dev/null

    (
        python agent/optuna_lgbm_search_v2.py \
            --target TARGET_TBM_LONG \
            --data "$TBM_DATA" \
            --ml-metric logloss \
            --objective focal \
            --n-trials "$N_TRIALS" \
            --n-jobs 1 \
            --study-name "$EXP2_STUDY" \
            --db-dir "$DB_DIR" \
            --train-cutoff-date "$CUTOFF" \
            --max-depth-range $MAX_DEPTH_MIN $MAX_DEPTH_MAX \
            --num-leaves-range $NUM_LEAVES_MIN $NUM_LEAVES_MAX \
            --max-n-estimators $MAX_N_ESTIMATORS \
            --early-stopping-rounds $EARLY_STOPPING \
            --num-threads $THREADS_PER_WORKER \
            --max-folds $MAX_FOLDS
    ) 2>&1 | tee -a "$LOG"
    EXP2_EXIT=${PIPESTATUS[0]}

    if [ $EXP2_EXIT -eq 0 ]; then
        EXP2_STATUS="PASSED"
        echo "  ✓ EXP 2 PASSED" | tee -a "$LOG"
    else
        EXP2_STATUS="FAILED (search, exit $EXP2_EXIT)"
        echo "  ✗ EXP 2 FAILED (exit $EXP2_EXIT)" | tee -a "$LOG"
    fi

    # Upload Exp 2 artifacts
    gsutil -m cp ${DB_DIR}/${EXP2_STUDY}.journal "$BUCKET/$GCS_PREFIX/studies/" 2>/dev/null || true
fi
update_status
gsutil cp "$LOG" "$BUCKET/$GCS_PREFIX/logs/" 2>/dev/null || true

# =============================================================================
# EXPERIMENT 3: Volatility Expansion Prediction
# =============================================================================
echo "" | tee -a "$LOG"
echo "============================================================" | tee -a "$LOG"
echo " [EXP 3/3] VOLATILITY EXPANSION PREDICTION" | tee -a "$LOG"
echo "  Hypothesis: Predicting vol magnitude is easier than direction" | tee -a "$LOG"
echo "  Target: TARGET_VOL_EXPANSION (top-20% 24H True Range)" | tee -a "$LOG"
echo "  NOTE: Metrics only — no PnL backtest" | tee -a "$LOG"
echo "============================================================" | tee -a "$LOG"
EXP3_STATUS="RUNNING"
update_status

# Step 3a: Generate volatility target
VOL_DATA="/home/$(whoami)/data/${DATASET_NAME}_vol.parquet"
echo "  Generating Vol Expansion target..." | tee -a "$LOG"
(
    python scripts/generate_vol_target.py \
        --data "$DATA" \
        --output "$VOL_DATA" \
        --horizon 288 \
        --rolling-window 10080 \
        --percentile 0.80
) 2>&1 | tee -a "$LOG"
VOL_GEN_EXIT=${PIPESTATUS[0]}

if [ $VOL_GEN_EXIT -ne 0 ]; then
    EXP3_STATUS="FAILED (target generation, exit $VOL_GEN_EXIT)"
    echo "  ✗ EXP 3 FAILED at target generation" | tee -a "$LOG"
else
    # Step 3b: Run Optuna search on volatility target
    EXP3_STUDY="${GCS_PREFIX}_exp3_vol"
    rm -f "${DB_DIR}/${EXP3_STUDY}.journal" 2>/dev/null

    (
        python agent/optuna_lgbm_search_v2.py \
            --target TARGET_VOL_EXPANSION \
            --data "$VOL_DATA" \
            --ml-metric logloss \
            --objective focal \
            --n-trials "$N_TRIALS" \
            --n-jobs 1 \
            --study-name "$EXP3_STUDY" \
            --db-dir "$DB_DIR" \
            --train-cutoff-date "$CUTOFF" \
            --max-depth-range $MAX_DEPTH_MIN $MAX_DEPTH_MAX \
            --num-leaves-range $NUM_LEAVES_MIN $NUM_LEAVES_MAX \
            --max-n-estimators $MAX_N_ESTIMATORS \
            --early-stopping-rounds $EARLY_STOPPING \
            --num-threads $THREADS_PER_WORKER \
            --max-folds $MAX_FOLDS
    ) 2>&1 | tee -a "$LOG"
    EXP3_EXIT=${PIPESTATUS[0]}

    if [ $EXP3_EXIT -eq 0 ]; then
        EXP3_STATUS="PASSED"
        echo "  ✓ EXP 3 PASSED" | tee -a "$LOG"
    else
        EXP3_STATUS="FAILED (search, exit $EXP3_EXIT)"
        echo "  ✗ EXP 3 FAILED (exit $EXP3_EXIT)" | tee -a "$LOG"
    fi

    # Upload Exp 3 artifacts
    gsutil -m cp ${DB_DIR}/${EXP3_STUDY}.journal "$BUCKET/$GCS_PREFIX/studies/" 2>/dev/null || true
fi
update_status

# =============================================================================
# GENERATE OVERNIGHT_RESULTS.md
# =============================================================================
TOTAL_ELAPSED=$(( $(date +%s) - START_TIME ))
RESULTS_FILE="OVERNIGHT_RESULTS.md"

cat > "$RESULTS_FILE" << EOF
# Overnight Three-Prong Alpha Search Results

**Run completed:** $(date -Iseconds)
**Wall time:** $((TOTAL_ELAPSED / 3600))h $((TOTAL_ELAPSED % 3600 / 60))m
**Dataset:** $DATASET_NAME
**Trials per experiment:** $N_TRIALS

## Experiment Summary

| Experiment | Status | Hypothesis |
|---|---|---|
| Exp 1: Asymmetric Loss | $EXP1_STATUS | 5× FP penalty → higher precision |
| Exp 2: TBM Target | $EXP2_STATUS | Triple Barrier 1.5×/1.0× ATR, 72H |
| Exp 3: Vol Expansion | $EXP3_STATUS | Predict top-20% TR magnitude |

## Detailed Results

### Exp 1: Asymmetric Loss Function
- Status: $EXP1_STATUS
- Target: $TARGET_STANDARD
- Objective: asymmetric (5× FP penalty)
- Study: $EXP1_STUDY

### Exp 2: Triple Barrier Method
- Status: $EXP2_STATUS
- Target: TARGET_TBM_LONG (TP=1.5×ATR, SL=1.0×ATR, 72H vertical)
- Objective: focal (standard)
- Study: ${EXP2_STUDY:-N/A}

### Exp 3: Volatility Expansion
- Status: $EXP3_STATUS
- Target: TARGET_VOL_EXPANSION (top-20% 24H True Range)
- Objective: focal (standard)
- Study: ${EXP3_STUDY:-N/A}
- Note: Metrics only — no PnL backtest (direction-agnostic)

## Raw Metrics

Check the individual study journals and optuna CSV exports in GCS:
\`\`\`
gsutil -m cp -r ${BUCKET}/${GCS_PREFIX}/ ./overnight_alpha_results/
\`\`\`

## Recommendation

_To be filled in by manual review of the above metrics._
EOF

echo "" | tee -a "$LOG"
echo "============================================================" | tee -a "$LOG"
echo " OVERNIGHT RUN COMPLETE" | tee -a "$LOG"
echo "  Total wall time: $((TOTAL_ELAPSED / 3600))h $((TOTAL_ELAPSED % 3600 / 60))m" | tee -a "$LOG"
echo "  Exp 1 (Asymmetric):  $EXP1_STATUS" | tee -a "$LOG"
echo "  Exp 2 (TBM):         $EXP2_STATUS" | tee -a "$LOG"
echo "  Exp 3 (Vol):         $EXP3_STATUS" | tee -a "$LOG"
echo "  Results:             $RESULTS_FILE" | tee -a "$LOG"
echo "  GCS:                 $BUCKET/$GCS_PREFIX/" | tee -a "$LOG"
echo "============================================================" | tee -a "$LOG"

# Upload final artifacts
gsutil cp "$LOG" "$BUCKET/$GCS_PREFIX/logs/" 2>/dev/null || true
gsutil cp "$RESULTS_FILE" "$BUCKET/$GCS_PREFIX/" 2>/dev/null || true
gsutil -m cp reports/optuna_*.* "$BUCKET/$GCS_PREFIX/reports/" 2>/dev/null || true
update_status

# Shutdown VM if requested
if [ "$SHUTDOWN" = true ]; then
    echo "Shutting down VM in 30 seconds..." | tee -a "$LOG"
    gsutil cp "$LOG" "$BUCKET/$GCS_PREFIX/logs/" 2>/dev/null || true
    sleep 30
    sudo shutdown -h now
fi
