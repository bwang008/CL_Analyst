"""E-number ordering tests for generate_ensemble_artifacts (mis-sourcing fix).

Root cause being fixed: E01..E04 labels were assigned by enumeration position
after a sort whose key dropped the metric. When every pair comes from ONE sweep
(canary), the keys tie and Python's stable sort falls back to opt_data insertion
order — the ProcessPoolExecutor as_completed() order, which is non-deterministic
and unrelated to top_pairs.json. That mislabeled the emitted predictions/configs.

The fix drives E-order from top_pairs.json (the canonical selection), with a
deterministic metric-aware fallback when top_pairs.json is absent.
"""

import json
import os

from agent.generate_ensemble_artifacts import (
    _canonical_pair_order,
    _ensemble_sort_key,
)

SWEEP = "sweep_hs14b_2x1_6h_canary_20260701-0703"


def _pk(long_metric, short_metric):
    return (
        f"oos_predictions_{SWEEP}_long_{long_metric}"
        f"|oos_predictions_{SWEEP}_short_{short_metric}"
    )


# Canonical top_pairs order used across the suite: LL/LL, LL/AP, AP/LL, AP/AP
CANONICAL = [
    ("logloss", "logloss"),
    ("logloss", "average_precision"),
    ("average_precision", "logloss"),
    ("average_precision", "average_precision"),
]


def _write_top_pairs(batch_dir):
    top_pairs = [
        {
            "target_long": f"oos_predictions_{SWEEP}_long_{lm}",
            "target_short": f"oos_predictions_{SWEEP}_short_{sm}",
        }
        for lm, sm in CANONICAL
    ]
    with open(os.path.join(batch_dir, "top_pairs.json"), "w") as f:
        json.dump(top_pairs, f)


def _scrambled_opt_data(order):
    """Build opt_data with the given insertion order (simulates as_completed)."""
    return {_pk(lm, sm): {"metrics": {"total_pnl": i}} for i, (lm, sm) in enumerate(order)}


def test_eorder_follows_top_pairs_regardless_of_insertion_order(tmp_path):
    batch_dir = str(tmp_path)
    _write_top_pairs(batch_dir)
    # Insertion order deliberately scrambled vs canonical (like a real as_completed)
    scrambled = [
        ("average_precision", "logloss"),
        ("average_precision", "average_precision"),
        ("logloss", "logloss"),
        ("logloss", "average_precision"),
    ]
    opt_data = _scrambled_opt_data(scrambled)
    ordered = _canonical_pair_order(opt_data, batch_dir)
    assert [k for k, _ in ordered] == [_pk(lm, sm) for lm, sm in CANONICAL]


def test_eorder_deterministic_across_two_insertion_orders(tmp_path):
    batch_dir = str(tmp_path)
    _write_top_pairs(batch_dir)
    a = _canonical_pair_order(
        _scrambled_opt_data(list(reversed(CANONICAL))), batch_dir
    )
    b = _canonical_pair_order(
        _scrambled_opt_data(CANONICAL), batch_dir
    )
    assert [k for k, _ in a] == [k for k, _ in b]


def test_shared_long_model_are_adjacent_per_top_pairs(tmp_path):
    """E01 & E02 (canonical) share long=logloss; E03 & E04 share long=AP."""
    batch_dir = str(tmp_path)
    _write_top_pairs(batch_dir)
    opt_data = _scrambled_opt_data(list(reversed(CANONICAL)))
    ordered = _canonical_pair_order(opt_data, batch_dir)
    long_metrics = [k.split("|")[0].split("_long_")[-1] for k, _ in ordered]
    assert long_metrics == ["logloss", "logloss", "average_precision", "average_precision"]


def test_fallback_sort_is_metric_aware_and_deterministic():
    """No top_pairs.json -> deterministic metric-aware sort (never ties on one sweep)."""
    opt_data = _scrambled_opt_data(list(reversed(CANONICAL)))
    ordered = sorted(opt_data.items(), key=_ensemble_sort_key)
    # logloss < average_precision on both sides -> canonical order
    assert [k for k, _ in ordered] == [_pk(lm, sm) for lm, sm in CANONICAL]


def test_canonical_order_falls_back_when_top_pairs_missing(tmp_path):
    """Missing top_pairs.json must not crash; falls back to deterministic sort."""
    batch_dir = str(tmp_path)  # no top_pairs.json written
    opt_data = _scrambled_opt_data(list(reversed(CANONICAL)))
    ordered = _canonical_pair_order(opt_data, batch_dir)
    assert [k for k, _ in ordered] == [_pk(lm, sm) for lm, sm in CANONICAL]


def test_leftover_pairs_not_in_top_pairs_are_appended_deterministically(tmp_path):
    """opt_data pairs absent from top_pairs.json are appended in sorted order,
    never dropped, and after the declared pairs."""
    batch_dir = str(tmp_path)
    # top_pairs declares only the first two canonical pairs
    top_pairs = [
        {
            "target_long": f"oos_predictions_{SWEEP}_long_{lm}",
            "target_short": f"oos_predictions_{SWEEP}_short_{sm}",
        }
        for lm, sm in CANONICAL[:2]
    ]
    with open(os.path.join(batch_dir, "top_pairs.json"), "w") as f:
        json.dump(top_pairs, f)
    opt_data = _scrambled_opt_data(list(reversed(CANONICAL)))
    ordered = _canonical_pair_order(opt_data, batch_dir)
    # First two must be the declared canonical pairs, in top_pairs order
    assert [k for k, _ in ordered[:2]] == [_pk(*CANONICAL[0]), _pk(*CANONICAL[1])]
    # All four still present, no drops
    assert set(k for k, _ in ordered) == set(opt_data.keys())
    assert len(ordered) == 4
