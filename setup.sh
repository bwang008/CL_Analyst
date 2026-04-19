#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ ! -d "${PROJECT_ROOT}/.venv" ]; then
  python3 -m venv "${PROJECT_ROOT}/.venv"
fi

source "${PROJECT_ROOT}/.venv/bin/activate"
python -m pip install --upgrade pip
python -m pip install -r "${PROJECT_ROOT}/requirements.txt"

if [ ! -f "${PROJECT_ROOT}/.env" ]; then
  cp "${PROJECT_ROOT}/.env.example" "${PROJECT_ROOT}/.env"
  echo "Created .env from .env.example (edit CL_DATA_ROOT)."
fi

source "${PROJECT_ROOT}/.env" || true

if [ -z "${CL_DATA_ROOT:-}" ]; then
  echo "ERROR: CL_DATA_ROOT is not set in .env"
  exit 1
fi

SEED_PATH="${CL_DATA_ROOT}/data/raw/cl-5m_bk.csv"
if [ ! -f "${SEED_PATH}" ]; then
  echo "ERROR: Missing seed CSV at ${SEED_PATH}"
  echo "See docs/BOOTSTRAP_DATA.md for acquisition steps."
  exit 1
fi

echo "Setup complete."
echo "Next: python scripts/tier0_checks.py --config configs/strategies/hourly_ensemble_004.json"
echo "Then: python -m src.live_execution.live_trader --config configs/strategies/hourly_ensemble_004.json --dry-run"
