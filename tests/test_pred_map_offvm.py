"""Off-VM merge dry-run tests for Fix G (prediction filename collision).

Simulates the reproducibility blocker: individual-mode sweeps write GENERIC
filenames ``oos_predictions_sweep_{side}_{metric}.csv`` (identical across ALL
sweeps) into ``registry/production_output/``. batch_progress.json stores Linux
``local_dir`` paths, but the merge is re-run OFF-VM on Windows — so the old
``startswith`` re-keying failed and predictions could be mis-sourced.

These tests build a real on-disk tree for two sweeps, feed LINUX-style local_dir
values in the (simulated) progress data, and assert that build_pred_map resolves
the CORRECT per-sweep file for each with NO collision.
"""

import os

from agent.batch_post_optimizer import build_pred_map


def _make_sweep(base_dir, prefix, long_content, short_content):
    """Create reports/<prefix>/registry/production_output/ with generic pred files."""
    prod = os.path.join(base_dir, "reports", prefix, "registry", "production_output")
    os.makedirs(prod, exist_ok=True)
    long_path = os.path.join(prod, "oos_predictions_sweep_long_logloss.csv")
    short_path = os.path.join(prod, "oos_predictions_sweep_short_logloss.csv")
    with open(long_path, "w") as f:
        f.write(long_content)
    with open(short_path, "w") as f:
        f.write(short_content)
    return prod, long_path, short_path


def test_offvm_merge_no_collision(tmp_path):
    """Two sweeps with identical generic filenames must resolve to distinct files."""
    base = str(tmp_path)
    prefix_a = "sweep_hs14a_2x1_6h_scout"
    prefix_b = "sweep_hs14a_3x1_6h_scout"

    _, a_long, a_short = _make_sweep(base, prefix_a, "A_LONG", "A_SHORT")
    _, b_long, b_short = _make_sweep(base, prefix_b, "B_LONG", "B_SHORT")

    # Simulate batch_progress.json: local_dir stored as LINUX paths (as
    # vm_post_optimize.sh rewrites them), which do NOT exist on this Windows/local FS.
    # The re-keying must NOT depend on these — it matches by gcs_prefix path segment.
    linux_local_dirs = [
        f"/home/runner/project/reports/{prefix_a}",
        f"/home/runner/project/reports/{prefix_b}",
    ]
    # search_dirs are the REAL local dirs we actually walk (off-VM layout).
    search_dirs = [
        os.path.join(base, "reports", prefix_a),
        os.path.join(base, "reports", prefix_b),
    ]
    gcs_prefixes = [prefix_a, prefix_b]

    # (linux_local_dirs is intentionally unused by build_pred_map — that's the point:
    # resolution is path-agnostic and does not touch the progress-json paths.)
    assert all(not os.path.exists(p) for p in linux_local_dirs)

    pred_map = build_pred_map(search_dirs, gcs_prefixes)

    key_a_long = f"oos_predictions_{prefix_a}_long_logloss"
    key_a_short = f"oos_predictions_{prefix_a}_short_logloss"
    key_b_long = f"oos_predictions_{prefix_b}_long_logloss"
    key_b_short = f"oos_predictions_{prefix_b}_short_logloss"

    # All four unique keys must be present and point to the CORRECT per-sweep file.
    for key in (key_a_long, key_a_short, key_b_long, key_b_short):
        assert key in pred_map, f"missing unique key {key}"

    assert os.path.normpath(pred_map[key_a_long]) == os.path.normpath(a_long)
    assert os.path.normpath(pred_map[key_a_short]) == os.path.normpath(a_short)
    assert os.path.normpath(pred_map[key_b_long]) == os.path.normpath(b_long)
    assert os.path.normpath(pred_map[key_b_short]) == os.path.normpath(b_short)

    # Content sanity: each resolved file has its own sweep's content (no cross-source).
    assert open(pred_map[key_a_long]).read() == "A_LONG"
    assert open(pred_map[key_b_long]).read() == "B_LONG"
    assert open(pred_map[key_a_short]).read() == "A_SHORT"
    assert open(pred_map[key_b_short]).read() == "B_SHORT"


def test_offvm_longest_prefix_wins(tmp_path):
    """A prefix that is a segment-substring of another must not shadow the longer one."""
    base = str(tmp_path)
    short_prefix = "sweep_hs14a"
    long_prefix = "sweep_hs14a_3x1_6h_scout"

    _, s_long, _ = _make_sweep(base, short_prefix, "SHORT_PFX_LONG", "SHORT_PFX_SHORT")
    _, l_long, _ = _make_sweep(base, long_prefix, "LONG_PFX_LONG", "LONG_PFX_SHORT")

    search_dirs = [
        os.path.join(base, "reports", short_prefix),
        os.path.join(base, "reports", long_prefix),
    ]
    # Deliberately unsorted; build_pred_map must sort longest-first internally.
    gcs_prefixes = [short_prefix, long_prefix]

    pred_map = build_pred_map(search_dirs, gcs_prefixes)

    assert os.path.normpath(
        pred_map[f"oos_predictions_{long_prefix}_long_logloss"]
    ) == os.path.normpath(l_long)
    assert os.path.normpath(
        pred_map[f"oos_predictions_{short_prefix}_long_logloss"]
    ) == os.path.normpath(s_long)
    assert open(pred_map[f"oos_predictions_{long_prefix}_long_logloss"]).read() == "LONG_PFX_LONG"
