#!/bin/bash
cd /home/bwang/project
source /opt/optuna-env/bin/activate
python gcp/vm_e2e_pipeline.py --data /home/bwang/data/cl-5m_bk_set_11.parquet \
  --train-cutoff-date 2022-01-01 \
  --strategy-config configs/strategies/ensemble4.json \
  --db-dir models/optuna_studies \
  --output-dir canary_output \
  --gcs-bucket gs://cltrainer-optuna-results \
  --gcs-prefix canary \
  --metrics logloss f0.5 \
  --study-prefix canary \
  --targets TARGET_TRIPLE_1.5x0.75_12H_LONG TARGET_TRIPLE_1.5x0.75_12H_SHORT > manual_e2e.log 2>&1
