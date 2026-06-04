"""Centralized strategy config loader with global risk filter inheritance.

Provides ``load_strategy_config()`` — the single entry-point for reading a
strategy JSON file and transparently merging house-rules from
``configs/global_risk_filters.json``.

Merge behaviour:
    * Global filter keys are added to the strategy dict **only when absent**.
    * If the strategy sets ``"override_global_filters": true``, global filters
      are skipped entirely.
    * Per-strategy overrides of individual keys (e.g. a strategy that only wants
      ``blocked_entry_hours_est: [8]``) are respected.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Union

log = logging.getLogger(__name__)

# Project root: four levels up from this file
# src/live_execution/config_loader.py → src/live_execution → src → project root
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_GLOBAL_FILTERS_PATH = _PROJECT_ROOT / "configs" / "global_risk_filters.json"

# Keys that are eligible for inheritance from global_risk_filters.json
_INHERITABLE_KEYS = (
    "blocked_entry_hours_est",
    "blocked_entry_hours_by_day",
    "block_long_weekends",
    "long_weekend_block_scope",
)


def _load_global_filters() -> dict:
    """Load the global risk filters JSON, returning {} if not found."""
    if not _GLOBAL_FILTERS_PATH.exists():
        log.warning(
            "[ConfigLoader] ⚠ global_risk_filters.json NOT FOUND at %s — "
            "ExecutionGuard risk filters will NOT be applied. "
            "If this is a deployed environment, ensure the file is present.",
            _GLOBAL_FILTERS_PATH,
        )
        return {}
    with open(_GLOBAL_FILTERS_PATH) as f:
        data = json.load(f)
    log.info(
        "[ConfigLoader] Loaded global risk filters: %s",
        {k: v for k, v in data.items() if k != "override_global_filters"},
    )
    return data


def load_strategy_config(config_path: Union[str, Path]) -> dict:
    """Load a strategy JSON and merge global risk filters.

    Parameters
    ----------
    config_path : str | Path
        Path to the strategy configuration JSON file.

    Returns
    -------
    dict
        The strategy config dict with global risk filter keys merged in
        (unless ``override_global_filters`` is set to ``true``).
    """
    config_path = Path(config_path)
    with open(config_path) as f:
        cfg = json.load(f)

    # If the strategy explicitly opts out, return as-is
    if cfg.get("override_global_filters", False):
        log.info(
            "[ConfigLoader] Strategy '%s' has override_global_filters=true — "
            "skipping global filter merge.",
            cfg.get("nickname", config_path.stem),
        )
        return cfg

    # Load and merge global filters
    global_filters = _load_global_filters()
    if not global_filters:
        return cfg

    merged_count = 0
    for key in _INHERITABLE_KEYS:
        if key not in cfg and key in global_filters:
            cfg[key] = global_filters[key]
            merged_count += 1

    if merged_count > 0:
        log.info(
            "[ConfigLoader] Merged %d global risk filter keys into '%s'",
            merged_count,
            cfg.get("nickname", config_path.stem),
        )

    return cfg
