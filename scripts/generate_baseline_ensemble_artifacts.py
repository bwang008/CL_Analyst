"""Render the BASELINE (pass-1 graft) ensemble artifacts for a batch's top-N pairs
(every pair in the arm's top_pairs.json — N is manifest-driven via
optuna.pair_selection_top_n; the historical default 2 per side yields 4 pairs).

Lineage: ticket pre-ensemble-artifacts_07122026_1712 (built as the "pre"
counterfactual renderer) — promoted to the DEFAULT pipeline product when the
pass-2 joint ensemble re-optimization was demoted to opt-in
(-EnsembleOptimization), after the 03B forensic showed pass-2 overfitting the
optimizer window (holdout −$81k shipped vs +$61k grafted baselines).

For each pair in the arm's top_pairs.json this script:
  1. reconstructs the pair's grafted pass-1 baseline via the SAME production
     path the pass-2 guard ships (agent.generate_ensemble_artifacts.
     _guard_shipped_pair_config -> batch_post_optimizer._build_pair_base_config,
     sourced from optimization_results_<obj>.json + batch_progress.json),
  2. stamps a full promotable strategy config (symbol/models/holdout — same
     stamping rules as agent/generate_ensemble_artifacts.py),
  3. copies the per-side sweep prediction CSVs into the batch dir,
  4. backtests it via agent/backtest_engine.py,
and emits STRICTLY ADDITIVE artifacts:

    <batch>/configs/baseline/<TAG>_<Obj>_E0N_baseline_<date>.json
    <batch>/predictions/baseline/<TAG>_<Obj>_E0N_baseline_{long,short}_predictions.csv
    <batch>/<obj>_baseline_backtests.md
    <batch>/batch_summary_baseline_<obj>.md

It does NOT require pass-2 to have run: the only inputs are pass-1 results,
batch_progress, top_pairs.json, the manifest and the sweep artifact trees.
When the pass-2 report exists (opt-in -EnsembleOptimization runs), E-slot
order is additionally hard-checked 1:1 against its headings.

--render-optimized (unchanged secondary mode): render the human-readable
companion batch_summary_optimized_<obj>_readable.md from the structured
optimization_results JSONs (the machine-contract markdowns parsed by
unified_pair_optimizer / compare_objective_arms are never touched).

House rules honored:
  * NO silent defaults — every missing input raises a loud, named error.
  * Every file write routes through assert_baseline_output_path so the
    additive guarantee is mechanical.

Usage (local; the VM chain invokes it with explicit --data/--exec-data):
    conda run -n trader python scripts/generate_baseline_ensemble_artifacts.py \
        --batch-dir reports/batch_runs/<batch>
    conda run -n trader python scripts/generate_baseline_ensemble_artifacts.py \
        --batch-dir reports/batch_runs/<batch> --render-optimized
"""

import argparse
import copy
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

# Ensure project root is on path for script-mode runs
# (python scripts/generate_baseline_ensemble_artifacts.py from the repo root / VM).
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Bind the REAL production helpers (identity-pinned by the test suite) —
# never re-implement pair parsing / layout / graft reconstruction here.
from agent.generate_ensemble_artifacts import (  # noqa: E402
    _canonical_pair_order,
    _guard_shipped_pair_config,
    _layout_subdir,
    _resolve_side_pred,
    parse_experiment_key,
    resolve_exec_data_path,
)
from src.core.dataset_tag import derive_dataset_tag  # noqa: E402
from src.core.instrument_master import dollars_per_point, get_instrument  # noqa: E402
from src.live_execution.instrument_context import resolve_instrument_context  # noqa: E402

UNPARSED = "UNPARSED"

GEOMETRY_KEYS = (
    "tp_atr_mult",
    "sl_atr_mult",
    "trailing_atr_mult",
    "trailing_sl_atr_offset",
    "cooldown_bars",
    "max_hold_bars",
    "consecutive_signal_threshold",
    "atr_period",
)


# ---------------------------------------------------------------------------
# Namespace write-guard (mechanical additive guarantee)
# ---------------------------------------------------------------------------


def assert_baseline_output_path(path):
    """Raise ValueError unless *path* is one of the sanctioned outputs.

    Allowed (and nothing else):
      * configs/baseline/*_baseline_*.json         (baseline configs)
      * predictions/baseline/*_baseline_*.csv      (per-side prediction copies)
      * batch_summary_baseline_<obj>.md            (baseline summary)
      * <obj>_baseline_backtests.md                (baseline backtests report)
      * batch_summary_optimized_<obj>_readable.md  (readable rendering of the
        optimized results — the machine-contract markdowns are NEVER written)

    Every file write in this module MUST route through this guard — it makes
    the strictly-additive guarantee mechanical rather than aspirational.
    """
    p = Path(path)
    name = p.name
    parents = [part.lower() for part in p.parts[:-1]]

    if re.fullmatch(r"batch_summary_baseline_[A-Za-z0-9_]+\.md", name):
        return None
    if re.fullmatch(r"[A-Za-z0-9_]+_baseline_backtests\.md", name):
        return None
    if re.fullmatch(r"batch_summary_optimized_[A-Za-z0-9_]+_readable\.md", name):
        return None
    if name.endswith(".json") and "_baseline_" in name:
        if len(parents) >= 2 and parents[-1] == "baseline" and parents[-2] == "configs":
            return None
        raise ValueError(
            f"Refusing to write baseline config outside configs/baseline/: {path}"
        )
    if name.endswith(".csv") and "_baseline_" in name:
        if len(parents) >= 2 and parents[-1] == "baseline" and parents[-2] == "predictions":
            return None
        raise ValueError(
            f"Refusing to write baseline predictions outside predictions/baseline/: {path}"
        )
    raise ValueError(
        f"Refusing to write non-baseline output path: {path}. This script is "
        "STRICTLY additive — allowed outputs are configs/baseline/*_baseline_*.json, "
        "predictions/baseline/*_baseline_*.csv, batch_summary_baseline_<obj>.md, "
        "<obj>_baseline_backtests.md and batch_summary_optimized_<obj>_readable.md only."
    )


def _write_text(path, text):
    assert_baseline_output_path(path)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    print(f"  [BASELINE] wrote {path}")


def _write_json(path, obj):
    assert_baseline_output_path(path)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=4), encoding="utf-8")
    print(f"  [BASELINE] wrote {path}")


def _copy_prediction(src, dst):
    assert_baseline_output_path(dst)
    if not os.path.isfile(src):
        raise FileNotFoundError(
            f"per-side prediction CSV not found: {src} — the sweep artifact "
            "tree must be present before baseline artifact generation"
        )
    p = Path(dst)
    p.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    print(f"  [BASELINE] wrote {dst}")


def _read_json(path):
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Pair selection (top_pairs.json is the authority) + heading parity
# ---------------------------------------------------------------------------


def load_top_pairs(batch_dir, objective="sharpe"):
    """Return (pair_key_list, pairs_filename, qualify_list). The arm's pairs
    file IS the E-slot order authority (same candidates as
    _canonical_pair_order).

    qualify_list[i] is {"long": bool|None, "short": bool|None} — the
    selector's passed_filter flags per leg. None means the pairs file
    predates the flags (legacy) and qualify status is UNKNOWN, which the
    report must say rather than guess.
    """
    candidates = (["top_pairs.json"] if objective == "sharpe"
                  else [f"top_pairs_{objective}.json", "top_pairs.json"])
    for name in candidates:
        path = os.path.join(batch_dir, name)
        if not os.path.isfile(path):
            continue
        pairs = _read_json(path)
        keys = [
            f"{p.get('target_long', '')}|{p.get('target_short', '')}"
            for p in pairs
        ]
        if not keys or any(k == "|" for k in keys):
            raise ValueError(
                f"{path} is empty or malformed — cannot derive baseline pairs"
            )
        qualify = [
            {
                "long": (bool(p["long_passed_filter"])
                         if "long_passed_filter" in p else None),
                "short": (bool(p["short_passed_filter"])
                          if "short_passed_filter" in p else None),
            }
            for p in pairs
        ]
        return keys, name, qualify
    raise ValueError(
        f"no pairs file found in {batch_dir} (tried: {', '.join(candidates)}) "
        "— pair selection must run before baseline artifact generation"
    )


def _pair_sweeps(pair_key):
    keys = pair_key.split("|")
    long_key = keys[0]
    short_key = keys[1] if len(keys) > 1 else ""
    long_sweep, _, long_metric = parse_experiment_key(long_key, "long")
    short_sweep, _, short_metric = parse_experiment_key(short_key, "short")
    return long_sweep, long_metric, short_sweep, short_metric


def _optimized_backtests_path(batch_dir, objective):
    return os.path.join(batch_dir, f"{objective}_ensemble_backtests.md")


def assert_order_matches_existing(pair_keys, batch_dir, objective="sharpe"):
    """Hard-fail unless *pair_keys* is 1:1 with the '## Ensemble N: L / S'
    headings of the existing <obj>_ensemble_backtests.md. Returns None."""
    md_path = _optimized_backtests_path(batch_dir, objective)
    if not os.path.isfile(md_path):
        raise ValueError(
            f"cannot verify E-slot order: {md_path} does not exist"
        )
    text = Path(md_path).read_text(encoding="utf-8-sig")
    headings = re.findall(
        r"^## Ensemble (\d+):\s*(\S+)\s*/\s*(\S+)\s*$", text, re.MULTILINE)
    if len(headings) != len(pair_keys):
        raise ValueError(
            f"E-slot count mismatch: {md_path} has {len(headings)} "
            f"'## Ensemble N:' headings but the pairs file has "
            f"{len(pair_keys)} pairs"
        )
    for idx, pair_key in enumerate(pair_keys, start=1):
        long_sweep, _, short_sweep, _ = _pair_sweeps(pair_key)
        num, h_long, h_short = headings[idx - 1]
        if int(num) != idx or h_long != long_sweep or h_short != short_sweep:
            raise ValueError(
                f"E-slot order mismatch at Ensemble {idx}: existing report "
                f"heading is 'Ensemble {num}: {h_long} / {h_short}' but the "
                f"pairs file says '{long_sweep} / {short_sweep}' "
                f"({md_path}) — refusing to emit misaligned baseline artifacts"
            )
    return None


# ---------------------------------------------------------------------------
# Baseline config construction (graft blocks are the shipped truth)
# ---------------------------------------------------------------------------


def build_baseline_config(skeleton_cfg, grafted_sides, nickname):
    """Attach the grafted pass-1 side blocks to a deep copy of the skeleton.

    PURE: no I/O, neither input is mutated. The grafted block is shipped
    VERBATIM (it is what batch_post_optimizer._build_pair_base_config
    produced — the same object the pass-2 guard ships); the entry threshold
    is mirrored into models.<side>.threshold from tiers[0].min_prob (the
    field TieredEnsembleStrategy actually executes on).
    """
    out = copy.deepcopy(skeleton_cfg)
    for side in ("long", "short"):
        block = (grafted_sides or {}).get(side)
        if not isinstance(block, dict) or not block:
            raise ValueError(
                f"grafted config has no '{side}' side block for '{nickname}' "
                "— cannot build baseline config"
            )
        tiers = block.get("tiers") or []
        thr = tiers[0].get("min_prob") if tiers else None
        if thr is None:
            raise ValueError(
                f"grafted '{side}' block for '{nickname}' has no "
                "tiers[0].min_prob — the executable threshold is undefined; "
                "refusing"
            )
        out[side] = copy.deepcopy(block)
        if "models" not in out or side not in out["models"]:
            raise ValueError(
                f"skeleton config for '{nickname}' has no models.{side} "
                "block — cannot set entry threshold"
            )
        out["models"][side]["threshold"] = thr
    out["conflict_resolution"] = "hold"
    out["nickname"] = nickname
    out["description"] = (
        f"BASELINE (pass-1 graft) — {nickname}"
    )
    return out


def side_params_for_summary(block):
    """Extract the summary-table params from a grafted side block ('-' when a
    key is absent — the block itself is shipped verbatim either way)."""
    tiers = block.get("tiers") or []
    thr = tiers[0].get("min_prob") if tiers else block.get("entry_threshold")
    params = {"entry_threshold": thr if thr is not None else "-"}
    for key in GEOMETRY_KEYS:
        params[key] = block.get(key, "-")
    return params


# ---------------------------------------------------------------------------
# Engine stdout parsing (per-field loud UNPARSED fallback)
# ---------------------------------------------------------------------------

_TRADES_RE = re.compile(r"Total Trades:\s*([\d,]+)")
_PNL_RE = re.compile(r"Total Net PnL:\s*\$\s*(-?[\d,]+(?:\.\d+)?)")
_DD_RE = re.compile(r"Max Drawdown:\s*\$\s*(-?[\d,]+(?:\.\d+)?)")
_AB_TRADES_RE = re.compile(r"Trade Count\s+(-?[\d,]+)\s+(-?[\d,]+)")
_AB_PNL_RE = re.compile(
    r"Total Net PnL\s+\$\s*(-?[\d,]+(?:\.\d+)?)\s+\$\s*(-?[\d,]+(?:\.\d+)?)")
_AB_DD_RE = re.compile(
    r"Max Drawdown\s+\$\s*(-?[\d,]+(?:\.\d+)?)\s+\$\s*(-?[\d,]+(?:\.\d+)?)")


def _parse_int(pattern, segment):
    m = pattern.search(segment) if segment else None
    if not m:
        return UNPARSED
    try:
        return int(m.group(1).replace(",", ""))
    except (TypeError, ValueError):
        return UNPARSED


def _parse_float(pattern, segment):
    m = pattern.search(segment) if segment else None
    if not m:
        return UNPARSED
    try:
        return float(m.group(1).replace(",", ""))
    except (TypeError, ValueError):
        return UNPARSED


def parse_engine_output(stdout):
    """Parse agent/backtest_engine.py stdout into the summary fields.

    Keys: full_pnl, full_trades, full_maxdd (Historical AGGREGATE SUMMARY),
    opt_pnl, opt_trades, opt_maxdd (A/B COMPARISON Optimizer Window column),
    holdout_pnl, holdout_trades, holdout_maxdd (Holdout AGGREGATE SUMMARY),
    holdout_monthly_breakdown (the HOLDOUT report's MONTHLY BREAKDOWN block
    only).

    Per-field loud fallback: anything unparseable == the exact string
    'UNPARSED' — never zero/blank/None.
    """
    text = stdout or ""
    hold_idx = text.find("HOLDOUT REPORT")
    ab_idx = text.find("A/B COMPARISON")

    if hold_idx >= 0:
        hist_part = text[:hold_idx]
        hold_end = ab_idx if ab_idx > hold_idx else len(text)
        holdout_part = text[hold_idx:hold_end]
    else:
        hist_part = text[:ab_idx] if ab_idx >= 0 else text
        holdout_part = ""
    ab_part = text[ab_idx:] if ab_idx >= 0 else ""

    breakdown = UNPARSED
    if holdout_part:
        m = re.search(
            r"MONTHLY BREAKDOWN\s*\n(.*?)(?:\n[^\n]*EXIT DISTRIBUTION"
            r"|\n[^\n]*NOTABLE PERIODS|\Z)",
            holdout_part, re.DOTALL)
        if m:
            breakdown = "MONTHLY BREAKDOWN\n" + m.group(1).strip("\n")

    return {
        "full_pnl": _parse_float(_PNL_RE, hist_part),
        "full_trades": _parse_int(_TRADES_RE, hist_part),
        "full_maxdd": _parse_float(_DD_RE, hist_part),
        "opt_pnl": _parse_float(_AB_PNL_RE, ab_part),
        "opt_trades": _parse_int(_AB_TRADES_RE, ab_part),
        "opt_maxdd": _parse_float(_AB_DD_RE, ab_part),
        "holdout_pnl": _parse_float(_PNL_RE, holdout_part),
        "holdout_trades": _parse_int(_TRADES_RE, holdout_part),
        "holdout_maxdd": _parse_float(_DD_RE, holdout_part),
        "holdout_monthly_breakdown": breakdown,
    }


# ---------------------------------------------------------------------------
# Resolution chains (no silent defaults)
# ---------------------------------------------------------------------------


def _resolve_symbol(manifest, batch_dir):
    symbol = (manifest.get("baseline") or {}).get("symbol")
    if not symbol:
        raise ValueError(
            f"manifest baseline.symbol missing in {batch_dir}/manifest.json "
            "— refusing to default the instrument (economics would be wrong)"
        )
    return symbol


def _resolve_slippage(cli_slippage, manifest, batch_dir):
    if cli_slippage is not None:
        print(f"  [BASELINE] slippage source: CLI --slippage-per-side = {cli_slippage}")
        return cli_slippage
    slippage = (
        (manifest.get("baseline") or {})
        .get("execution_workflow", {})
        .get("slippage_per_side")
    )
    if slippage is None:
        raise ValueError(
            "no slippage available: pass --slippage-per-side or set "
            "baseline.execution_workflow.slippage_per_side in "
            f"{batch_dir}/manifest.json — refusing to default slippage"
        )
    print(f"  [BASELINE] slippage source: manifest "
          f"baseline.execution_workflow.slippage_per_side = {slippage}")
    return slippage


def _resolve_data_path(cli_data, manifest, batch_dir, objective, symbol):
    """CLI --data > manifest defaults.local_data_path > the manifest-derived
    data/processed/<dataset>.parquet > **Data** header of an existing
    baseline/optimized backtests md (re-runs) > ValueError."""
    tried = []
    if cli_data:
        data_path, source = cli_data, "CLI --data"
    else:
        data_path = (manifest.get("defaults") or {}).get("local_data_path")
        source = "manifest defaults.local_data_path"
        if not data_path:
            dv = ((manifest.get("baseline") or {}).get("data_workflow") or {}) \
                .get("dataset_version", "")
            if dv:
                # Mirror the deploy-chain naming: <SYM>_ prefix unless the
                # dataset_version already starts with the symbol.
                if dv.upper().startswith(str(symbol).upper()):
                    dataset_name = f"{dv}.parquet"
                else:
                    dataset_name = f"{symbol}_{dv}.parquet"
                candidate = os.path.join("data", "processed", dataset_name)
                tried.append(candidate)
                if os.path.isfile(candidate):
                    data_path = candidate
                    source = "manifest-derived data/processed path"
        if not data_path:
            for md_name in (f"{objective}_baseline_backtests.md",
                            f"{objective}_ensemble_backtests.md"):
                md_path = os.path.join(batch_dir, md_name)
                if not os.path.isfile(md_path):
                    continue
                text = Path(md_path).read_text(encoding="utf-8-sig")
                m = re.search(r"^\*\*Data\*\*:\s*(.+?)\s*$", text, re.MULTILINE)
                if m:
                    data_path = m.group(1)
                    source = f"**Data** header of {md_path}"
                    break
        if not data_path:
            raise ValueError(
                "no data path available: pass --data, set manifest "
                "defaults.local_data_path, or place the dataset at one of: "
                f"{tried or ['<manifest dataset_version missing>']}"
            )
    print(f"  [BASELINE] data source: {source} -> {data_path}")
    if not os.path.isfile(data_path):
        raise FileNotFoundError(
            f"Resolved data path {data_path} does not exist "
            f"(source: {source}). Download the dataset first."
        )
    return data_path


def _resolve_exec_source(cli_exec_data, manifest):
    """Return (exec_data_path_or_None, exec_source_string).

    CLI --exec-data > resolve_exec_data_path() in try/except; on miss fall
    back to the embedded EXEC_* columns (no --exec-data flag passed to the
    engine) and say so loudly in the exec-source string."""
    if cli_exec_data:
        if not os.path.isfile(cli_exec_data):
            raise FileNotFoundError(
                f"Resolved exec data path {cli_exec_data} does not exist "
                "(source: CLI). Download the raw exec parquet first."
            )
        return cli_exec_data, f"RAW exec parquet: {cli_exec_data} (CLI)"

    attempted = (
        (manifest.get("defaults") or {}).get("local_exec_data")
        or (manifest.get("baseline") or {})
        .get("execution_workflow", {})
        .get("execution_data_path")
        or "<none declared in manifest>"
    )
    try:
        resolved = resolve_exec_data_path(manifest, "")
    except Exception as exc:  # loud fallback, never fatal
        print(f"  [BASELINE] exec parquet unavailable ({exc}); "
              "falling back to embedded EXEC_* columns")
        return None, (
            "EMBEDDED EXEC_* columns (raw exec parquet unavailable locally: "
            f"{attempted})"
        )
    if resolved and os.path.isfile(resolved):
        return resolved, f"RAW exec parquet: {resolved}"
    return None, (
        "EMBEDDED EXEC_* columns (raw exec parquet unavailable locally: "
        f"{resolved or attempted})"
    )


def _resolve_base_config_path(manifest, batch_dir):
    scp = ((manifest.get("baseline") or {})
           .get("execution_workflow", {})
           .get("strategy_config_path"))
    if not scp:
        name = (manifest.get("defaults") or {}).get("strategy_config")
        if not name:
            raise ValueError(
                "no base strategy config: set baseline.execution_workflow."
                "strategy_config_path (v2) or defaults.strategy_config in "
                f"{batch_dir}/manifest.json — refusing any fallback"
            )
        scp = os.path.join("configs", "strategies", name)
    if not os.path.isfile(scp):
        raise FileNotFoundError(f"base strategy config not found: {scp}")
    return scp


def _resolve_holdout_months(manifest):
    holdout = (
        ((manifest.get("baseline") or {}).get("training_workflow") or {})
        .get("optuna", {})
        .get("post_optimizer_holdout_months")
    )
    if holdout is None:
        holdout = (manifest.get("defaults") or {}).get("post_optimizer_holdout_months")
    if holdout is None:
        raise ValueError(
            "post_optimizer_holdout_months not found in manifest "
            "(baseline.training_workflow.optuna or defaults) — refusing to default."
        )
    return holdout


def _derive_tag_and_date(batch_dir, manifest):
    """Same TAG/date derivation as agent/generate_ensemble_artifacts.py."""
    batch_name = os.path.basename(os.path.normpath(batch_dir))
    m_date = re.search(r"_(\d{8})_", batch_name)
    if m_date:
        ymd = m_date.group(1)
        mmddyyyy = f"{ymd[4:6]}{ymd[6:8]}{ymd[0:4]}"
    else:
        mmddyyyy = "00000000"
    parts = batch_name.split("_")
    if len(parts) > 3:
        dataset_tag = parts[3]
    else:
        experiments = manifest.get("experiments", [])
        if experiments and "label" in experiments[0]:
            dataset_tag = experiments[0]["label"].split(" ")[0]
        else:
            dataset_tag = "TAG"
    return dataset_tag, mmddyyyy


def _grafted_sides(batch_dir, objective, pair_key, base_config_path):
    """Reconstruct the pair's grafted pass-1 baseline via the production path
    (_guard_shipped_pair_config -> _build_pair_base_config). FATAL when the
    pass-1 inputs are unavailable — never a silent base-config fallback."""
    tmp_dir = tempfile.mkdtemp(prefix="baseline_graft_")
    out_path = os.path.join(tmp_dir, "pair_graft_src.json")
    shipped = _guard_shipped_pair_config(
        batch_dir, objective, pair_key, base_config_path, out_path)
    if shipped is None:
        raise ValueError(
            f"cannot reconstruct the pass-1 graft for pair {pair_key}: "
            f"optimization_results_{objective}.json / batch_progress.json "
            "unavailable — refusing any fallback"
        )
    return shipped


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------


def _fmt_num(v):
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


def _fmt_pnl(v):
    if v == UNPARSED:
        return UNPARSED
    return f"${v:,.2f}"


def _fmt_cell(v):
    if v == UNPARSED:
        return UNPARSED
    return str(v)


def _fmt_recovery(pnl, maxdd):
    """Recovery factor: PnL / |MaxDD| — the fixed-window Calmar/MAR analog.

    UNPARSED propagates (never a fabricated number); a zero drawdown (only
    possible with no losing excursion at all) renders '-' rather than inf.
    """
    if pnl == UNPARSED or maxdd == UNPARSED:
        return UNPARSED
    if maxdd == 0:
        return "-"
    return f"{pnl / abs(maxdd):.2f}"


def _render_md_table(headers, rows, right_align=()):
    """Render a markdown table whose raw text is column-aligned.

    Cells are padded to the widest entry per column so the pipes line up in
    plain text; `right_align` is a set of column indices rendered with a
    trailing-colon separator (numeric columns).
    """
    cols = len(headers)
    widths = [len(str(headers[c])) for c in range(cols)]
    for row in rows:
        for c in range(cols):
            widths[c] = max(widths[c], len(str(row[c])))

    def _pad(val, c):
        s = str(val)
        return s.rjust(widths[c]) if c in right_align else s.ljust(widths[c])

    lines = ["| " + " | ".join(_pad(headers[c], c) for c in range(cols)) + " |"]
    lines.append("|" + "|".join(
        ("-" * (widths[c] + 1) + ":") if c in right_align
        else "-" * (widths[c] + 2)
        for c in range(cols)) + "|")
    for row in rows:
        lines.append(
            "| " + " | ".join(_pad(row[c], c) for c in range(cols)) + " |")
    return lines


def _metric_desc(metric, side):
    short = metric.replace("average_precision", "AP").replace("logloss", "LL")
    return f"{short.upper()}_{side.upper()}"


def _header_lines(title, batch_name, exec_source, slippage, data_path,
                  generation_cmd, provenance, order_contract):
    return [
        f"# {title}",
        f"**Batch**: {batch_name}",
        f"**Generated**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"**Params Provenance**: {provenance}",
        f"**Order Contract**: {order_contract}",
        f"**Data**: {data_path}",
        f"**Exec Source**: {exec_source} | Slippage: {slippage}",
        f"**Generation Command**: `{generation_cmd}`",
        "",
        "---",
        "",
    ]


# ---------------------------------------------------------------------------
# Baseline artifact generation (per objective)
# ---------------------------------------------------------------------------


def _run_objective(batch_dir, objective, args, manifest, symbol,
                   slippage, contract_multiplier):
    batch_name = os.path.basename(os.path.normpath(batch_dir))

    pair_keys, pairs_file, pair_qualify = load_top_pairs(batch_dir, objective)
    print(f"  [BASELINE] {len(pair_keys)} pair(s) from {pairs_file}")

    # When the opt-in pass-2 report exists, the E-slot order must match it 1:1.
    if os.path.isfile(_optimized_backtests_path(batch_dir, objective)):
        assert_order_matches_existing(pair_keys, batch_dir, objective)
        print(f"  [BASELINE] E-slot order verified 1:1 against "
              f"{objective}_ensemble_backtests.md")
        order_contract = (
            f"E-slots follow {pairs_file}; verified 1:1 against "
            f"{objective}_ensemble_backtests.md headings"
        )
    else:
        order_contract = (
            f"E-slots follow {pairs_file} (pass-2 report absent — "
            "ensemble optimization not run for this batch)"
        )

    base_config_path = _resolve_base_config_path(manifest, batch_dir)
    base_config = _read_json(base_config_path)
    holdout_months = _resolve_holdout_months(manifest)
    tag, mmddyyyy = _derive_tag_and_date(batch_dir, manifest)

    data_path = _resolve_data_path(args.data, manifest, batch_dir, objective, symbol)
    exec_data, exec_source = _resolve_exec_source(args.exec_data, manifest)
    print(f"  [BASELINE] exec source: {exec_source}")

    data_basename = os.path.splitext(os.path.basename(data_path))[0]
    e2e_prefix = f"E2E_{derive_dataset_tag(data_basename, symbol)}"

    generation_cmd = (
        "python scripts/generate_baseline_ensemble_artifacts.py "
        f"--batch-dir {batch_dir}"
    )
    provenance = (
        "pass-1 winners grafted per pair via batch_post_optimizer."
        "_build_pair_base_config (the SAME graft the pass-2 guard ships), "
        f"sourced from optimization_results_{objective}.json + "
        "batch_progress.json"
    )

    bt_lines = _header_lines(
        f"Ensemble Backtests (BASELINE — pass-1 graft, {objective.capitalize()})",
        batch_name, exec_source, slippage, data_path, generation_cmd,
        provenance, order_contract)
    engine_script = os.path.join(PROJECT_ROOT, "agent", "backtest_engine.py")

    result_rows = []
    param_rows = []
    details = []
    for idx, pair_key in enumerate(pair_keys, start=1):
        long_sweep, long_metric, short_sweep, short_metric = _pair_sweeps(pair_key)
        long_desc = _metric_desc(long_metric, "long")
        short_desc = _metric_desc(short_metric, "short")
        experiment = f"{long_sweep} / {short_sweep}"

        grafted = _grafted_sides(batch_dir, objective, pair_key, base_config_path)

        cfg_stem = f"{tag}_{objective.capitalize()}_E{idx:02d}_baseline"
        cfg_name = f"{cfg_stem}_{mmddyyyy}.json"
        nickname = f"{cfg_stem}_{mmddyyyy}"

        # Skeleton: base config + the same stamping rules as
        # agent/generate_ensemble_artifacts.py main().
        skeleton = json.loads(json.dumps(base_config))
        skeleton["execution_symbol"] = symbol
        skeleton["holdout_months"] = holdout_months
        if "models" not in skeleton:
            skeleton["models"] = {"long": {}, "short": {}}

        pred_paths = {}
        for side, sweep, metric in (("long", long_sweep, long_metric),
                                    ("short", short_sweep, short_metric)):
            layout = _layout_subdir(sweep)
            side_key = f"oos_predictions_{sweep}_{side}_{metric}"
            src = _resolve_side_pred(sweep, layout, side, metric, side_key)
            pred_name = f"{cfg_stem}_{side}_predictions.csv"
            dst = os.path.join(batch_dir, "predictions", "baseline", pred_name)
            _copy_prediction(src, dst)
            pred_paths[side] = dst.replace("\\", "/")

            skeleton["models"][side]["experiment_id"] = f"{e2e_prefix}_{side}_{metric}"
            skeleton["models"][side]["model_path"] = (
                f"reports/{sweep}/registry/{layout}/registry/"
                f"{e2e_prefix}_{side}_{metric}/final_model.pkl"
            )
            skeleton["models"][side]["predictions_path"] = pred_paths[side]
            skeleton["models"][side]["symbol"] = symbol

        cfg = build_baseline_config(skeleton, grafted, nickname)

        # Post-emission self-check (same as generate_ensemble_artifacts):
        # a config that cannot start live is a batch failure.
        resolve_instrument_context(cfg)
        for side in ("long", "short"):
            mp = cfg["models"][side].get("model_path", "")
            if mp and not os.path.isfile(mp):
                print(f"  [BASELINE] WARN models.{side}.model_path not found "
                      f"on disk (may be un-synced): {mp}")

        cfg_path = os.path.join(batch_dir, "configs", "baseline", cfg_name)
        _write_json(cfg_path, cfg)

        # --- backtests md section (heading identity == pass-2 report shape)
        bt_lines.append(f"## Ensemble {idx}: {long_sweep} / {short_sweep}")
        bt_lines.append(
            f"**Long**: {long_desc} ({long_sweep}) | "
            f"**Short**: {short_desc} ({short_sweep})")
        bt_lines.append(f"**Config**: [{cfg_name}](configs/baseline/{cfg_name})")
        bt_lines.append(
            f"**Predictions**: [long](predictions/baseline/{cfg_stem}_long_predictions.csv) / "
            f"[short](predictions/baseline/{cfg_stem}_short_predictions.csv)")
        bt_lines.append("")
        bt_lines.append("### Verification Command")
        bt_lines.append("```bash")
        rel_cfg_path = cfg_path.replace("\\", "/")
        econ_flags = (
            f"--slippage-per-side {slippage} "
            f"--contract-multiplier {contract_multiplier}"
        )
        if exec_data:
            bt_lines.append(
                f'python agent/backtest_engine.py --config "{rel_cfg_path}" '
                f'--data "{data_path}" --exec-data "{exec_data}" {econ_flags}')
        else:
            bt_lines.append(
                f'python agent/backtest_engine.py --config "{rel_cfg_path}" '
                f'--data "{data_path}" {econ_flags}')
        bt_lines.append("```")
        bt_lines.append("")

        # --- engine run (exactly one per ensemble, E order)
        cmd = [
            sys.executable, engine_script,
            "--config", cfg_path,
            "--data", data_path,
            "--slippage-per-side", str(slippage),
            "--contract-multiplier", str(contract_multiplier),
        ]
        if exec_data:
            cmd.extend(["--exec-data", exec_data])
        print(f"  [BASELINE] running backtest for {objective} ensemble "
              f"E{idx:02d} ({cfg_name})...")
        result = subprocess.run(cmd, capture_output=True, text=True)
        stdout = result.stdout or ""
        stderr = getattr(result, "stderr", "") or ""

        bt_lines.append("### Backtest Results")
        bt_lines.append("```")
        bt_lines.append(stdout.strip())
        if stderr.strip():
            bt_lines.append("\nErrors:")
            bt_lines.append(stderr.strip())
        bt_lines.append("```")
        bt_lines.append("\n---")
        bt_lines.append("")

        parsed = parse_engine_output(stdout)
        if UNPARSED in parsed.values():
            unparsed_keys = [k for k, v in parsed.items() if v == UNPARSED]
            print(f"  [BASELINE] WARN E{idx:02d}: engine stdout unparseable for "
                  f"{unparsed_keys} — summary will carry UNPARSED markers "
                  "(raw output preserved in the backtests file)")

        qual = pair_qualify[idx - 1]
        if qual["long"] is None or qual["short"] is None:
            num_cell = f"E{idx:02d}"
        elif qual["long"] and qual["short"]:
            num_cell = f"E{idx:02d}"
        else:
            num_cell = f"E{idx:02d}*"
        result_rows.append([
            num_cell, experiment, f"{long_desc} + {short_desc}",
            _fmt_pnl(parsed["full_pnl"]), _fmt_pnl(parsed["opt_pnl"]),
            _fmt_pnl(parsed["holdout_pnl"]),
            _fmt_pnl(parsed["holdout_maxdd"]),
            _fmt_recovery(parsed["holdout_pnl"], parsed["holdout_maxdd"]),
            f"{_fmt_cell(parsed['full_trades'])} / "
            f"{_fmt_cell(parsed['opt_trades'])} / "
            f"{_fmt_cell(parsed['holdout_trades'])}",
        ])
        for side, side_desc in (("long", long_desc), ("short", short_desc)):
            sp = side_params_for_summary(cfg[side])
            param_rows.append([
                f"E{idx:02d}", side.capitalize(), side_desc,
                _fmt_num(sp["entry_threshold"]),
                _fmt_num(sp["tp_atr_mult"]),
                _fmt_num(sp["sl_atr_mult"]),
                _fmt_num(sp["trailing_atr_mult"]),
                _fmt_num(sp["trailing_sl_atr_offset"]),
                _fmt_num(sp["cooldown_bars"]),
                _fmt_num(sp["max_hold_bars"]),
                _fmt_num(sp["consecutive_signal_threshold"]),
                _fmt_num(sp["atr_period"]),
            ])

        details += [
            f"### Ensemble {idx}: {long_sweep} / {short_sweep}",
            f"**Experiment**: {experiment}",
            f"**Baseline Config**: [{cfg_name}](configs/baseline/{cfg_name})",
            "",
        ]
        details += _render_md_table(
            ["Window", "Net PnL", "MaxDD", "PnL/DD", "Trades"],
            [["Full", _fmt_pnl(parsed["full_pnl"]),
              _fmt_pnl(parsed["full_maxdd"]),
              _fmt_recovery(parsed["full_pnl"], parsed["full_maxdd"]),
              _fmt_cell(parsed["full_trades"])],
             ["Opt-window", _fmt_pnl(parsed["opt_pnl"]),
              _fmt_pnl(parsed["opt_maxdd"]),
              _fmt_recovery(parsed["opt_pnl"], parsed["opt_maxdd"]),
              _fmt_cell(parsed["opt_trades"])],
             ["Holdout", _fmt_pnl(parsed["holdout_pnl"]),
              _fmt_pnl(parsed["holdout_maxdd"]),
              _fmt_recovery(parsed["holdout_pnl"], parsed["holdout_maxdd"]),
              _fmt_cell(parsed["holdout_trades"])]],
            right_align={1, 2, 3, 4})
        details += [
            "",
            "**Holdout MONTHLY BREAKDOWN**:",
            "```",
            str(parsed["holdout_monthly_breakdown"]),
            "```",
            f"Full engine output: "
            f"[{objective}_baseline_backtests.md]({objective}_baseline_backtests.md)",
            "",
        ]

    bt_path = os.path.join(batch_dir, f"{objective}_baseline_backtests.md")
    _write_text(bt_path, "\n".join(bt_lines))

    sm_lines = _header_lines(
        f"Batch Summary (BASELINE — pass-1 graft, "
        f"{objective.capitalize()}) - {batch_name}",
        batch_name, exec_source, slippage, data_path, generation_cmd,
        provenance, order_contract)
    sm_lines += [
        f"Backtests report: [{objective}_baseline_backtests.md]"
        f"({objective}_baseline_backtests.md)",
        "",
        f"### Ensembles (Top {len(pair_keys)}) — BASELINE (pass-1 graft)",
        "",
    ]
    sm_lines += _render_md_table(
        ["#", "Experiment (Long / Short)", "Models",
         "PnL (full)", "PnL (opt-window)", "PnL (holdout)",
         "MaxDD (ho)", "PnL/DD (ho)",
         "Trades (full/opt/ho)"],
        result_rows, right_align={3, 4, 5, 6, 7, 8})
    if any(q["long"] is None or q["short"] is None for q in pair_qualify):
        sm_lines += [
            "",
            "_Qualify flags unavailable: this batch's pairs file predates "
            "the per-leg passed_filter annotation — re-run "
            "unified_pair_optimizer to annotate which slots were filled by "
            "penalized non-qualifying legs._",
        ]
    elif any(row[0].endswith("*") for row in result_rows):
        sm_lines += [
            "",
            "_\\* = includes a leg that FAILED the pair-selection qualify "
            "filter (needs opt PnL > 0, holdout PnL > 0, trades >= 100). "
            "The selector penalizes rather than drops non-qualifiers, so "
            "these slots are filled by negative-ranked legs — treat as "
            "what-if combos, not candidates._",
        ]
    sm_lines += [
        "",
        "### Per-side baseline parameters (pass-1 graft)",
        "",
    ]
    sm_lines += _render_md_table(
        ["#", "Side", "Model", "Thr", "TP", "SL", "Trail", "TrailOff",
         "Cooldown", "MaxHold", "Consec", "ATR"],
        param_rows, right_align={3, 4, 5, 6, 7, 8, 9, 10, 11})
    sm_lines += ["", "---", ""]
    sm_lines += details

    sm_path = os.path.join(batch_dir, f"batch_summary_baseline_{objective}.md")
    _write_text(sm_path, "\n".join(sm_lines))


# ---------------------------------------------------------------------------
# Readable rendering of the OPTIMIZED (shipped) results (--render-optimized)
#
# The machine-contract markdowns (batch_summary_optimized_<obj>[.|_ensembles_]
# md) are parsed positionally by agent/unified_pair_optimizer (VM pair
# selection), scripts/compare_objective_arms.py and the report tests — they
# must never be restructured. This mode renders a human-readable companion
# file from the structured optimization_results JSONs instead.
# ---------------------------------------------------------------------------


METRIC_SHORT = {"logloss": "LL", "average_precision": "AP"}
PARAM_KEY_ORDER = (
    "entry_threshold", "tp_atr_mult", "sl_atr_mult", "trailing_atr_mult",
    "trailing_sl_atr_offset", "cooldown_bars", "max_hold_bars",
    "consecutive_signal_threshold", "atr_period",
)


def ordered_pairs(opt_data, batch_dir, objective="sharpe"):
    """Canonical (pair_key, pair_val) E-slot order — delegates to the REAL
    agent.generate_ensemble_artifacts._canonical_pair_order (top_pairs.json
    authority). Dict insertion order must never drive E-slots."""
    return _canonical_pair_order(opt_data, batch_dir, objective)


def _fmt_money0(v):
    return "-" if v is None else f"${v:,.0f}"


def _fmt_pf(v):
    return "-" if v is None else f"{v:.2f}"


def _fmt_blocks(opt_info):
    bs = (opt_info or {}).get("block_sharpes")
    if not bs:
        return "-"
    return "/".join(f"{s:.1f}" for s in bs)


def _fmt_best_trial_readable(opt_info):
    if opt_info.get("regression_guard_triggered"):
        return "baseline (guard)"
    tn = opt_info.get("trial_number")
    nt = opt_info.get("n_trials")
    if tn is None or nt is None:
        return "-"
    return f"#{tn}/{nt}"


def _shipped_side_params(opt_info, side):
    """Mirror agent.batch_post_optimizer._side_params semantics: guard-kept
    pairs render the SHIPPED grafted baseline (tagged), everything else the
    pass-2 optimized side params. Returns (params_dict, source_tag)."""
    if opt_info.get("regression_guard_triggered"):
        side_params = (opt_info.get("baseline_side_params") or {}).get(side)
        return (side_params or {}), "baseline (guard)"
    sp = opt_info.get(f"{side}_params") or {}
    if sp:
        return sp, "optimized"
    suffix = f"_{side}"
    return {
        k[: -len(suffix)]: v
        for k, v in (opt_info.get("params") or {}).items()
        if k.endswith(suffix)
    }, "optimized"


def _param_cells(sp):
    return [_fmt_num(sp[k]) if k in sp else "-" for k in PARAM_KEY_ORDER]


def _natural_key(s):
    return [int(t) if t.isdigit() else t.lower()
            for t in re.split(r"(\d+)", str(s))]


def _run_readable_optimized(batch_dir, objective):
    """Render batch_summary_optimized_<obj>_readable.md (strictly additive)."""
    batch_name = os.path.basename(os.path.normpath(batch_dir))
    ind_path = os.path.join(
        batch_dir, f"optimization_results_{objective}.json")
    if not os.path.isfile(ind_path):
        raise ValueError(
            f"optimization_results_{objective}.json not found in batch dir: "
            f"{ind_path} — cannot render the readable optimized summary")
    ind_data = _read_json(ind_path)

    ens_path = os.path.join(
        batch_dir, f"optimization_results_ensembles_{objective}.json")
    ens_data = _read_json(ens_path) if os.path.isfile(ens_path) else None

    lines = [
        f"# Batch Summary (READABLE — optimized/shipped params, "
        f"{objective.capitalize()}) - {batch_name}",
        f"**Batch**: {batch_name}",
        f"**Generated**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"**Source**: rendered from `optimization_results_{objective}.json` "
        f"and `optimization_results_ensembles_{objective}.json`. The "
        f"machine-contract `batch_summary_optimized_{objective}.md` / "
        f"`batch_summary_optimized_ensembles_{objective}.md` are unchanged "
        "and remain the parsing authority (pair selection / A/B tooling).",
        "**Guard rows**: guard-kept pairs render their SHIPPED grafted "
        "baseline params, tagged `baseline (guard)`.",
        "**Generation Command**: `python "
        "scripts/generate_baseline_ensemble_artifacts.py --batch-dir "
        f"{batch_dir} --render-optimized`",
        "",
        "---",
        "",
    ]

    # --- Ensembles (pass-2) ------------------------------------------------
    if ens_data:
        ordered = ordered_pairs(ens_data, batch_dir, objective)
        res_rows, par_rows = [], []
        for idx, (pair_key, pair_val) in enumerate(ordered, start=1):
            oi = pair_val.get("optuna_info") or {}
            labels = pair_val.get("experiment_labels") or {}
            long_sweep, long_metric, short_sweep, short_metric = (
                _pair_sweeps(pair_key))
            long_desc = _metric_desc(long_metric, "long")
            short_desc = _metric_desc(short_metric, "short")
            experiment = (
                f"{labels.get('long_label', long_sweep)} / "
                f"{labels.get('short_label', short_sweep)}")
            om = pair_val.get("metrics") or {}
            bm = oi.get("baseline_metrics") or {}
            hm = oi.get("holdout_metrics") or {}
            res_rows.append([
                f"E{idx:02d}", experiment, f"{long_desc} + {short_desc}",
                _fmt_money0(bm.get("total_pnl")),
                _fmt_money0(om.get("total_pnl")),
                _fmt_money0(hm.get("total_pnl")),
                f"{bm.get('trade_count', '-')} / {om.get('trade_count', '-')}"
                f" / {hm.get('trade_count', '-')}",
                _fmt_blocks(oi), _fmt_best_trial_readable(oi),
            ])
            for side, side_desc, lbl_key in (
                    ("long", long_desc, "long_label"),
                    ("short", short_desc, "short_label")):
                sp, source = _shipped_side_params(oi, side)
                par_rows.append(
                    [f"E{idx:02d}", side.capitalize(),
                     f"{side_desc} @ {labels.get(lbl_key, pair_key)}"]
                    + _param_cells(sp) + [source])
        lines += [f"### Ensembles (Top {len(ordered)}) — shipped params", ""]
        lines += _render_md_table(
            ["#", "Experiment (Long / Short)", "Models", "PnL (pre)",
             "PnL (opt)", "PnL (holdout)", "Trades (pre/opt/ho)",
             "Block Sharpes", "Best Trial"],
            res_rows, right_align={3, 4, 5, 6})
        lines += ["", "### Ensemble per-side shipped parameters", ""]
        lines += _render_md_table(
            ["#", "Side", "Model", "Thr", "TP", "SL", "Trail", "TrailOff",
             "Cooldown", "MaxHold", "Consec", "ATR", "Source"],
            par_rows, right_align={3, 4, 5, 6, 7, 8, 9, 10, 11})
        lines += ["", "---", ""]
    else:
        lines += [
            f"_No optimization_results_ensembles_{objective}.json in this "
            "batch — ensembles section skipped._",
            "", "---", "",
        ]

    # --- Individual sides (pass-1) ------------------------------------------
    side_rank = {"long": 0, "short": 1}
    metric_rank = {"logloss": 0, "average_precision": 1}
    entries = []
    for key, val in ind_data.items():
        parts = key.split("|")
        if len(parts) != 3:
            continue
        label, side, metric = parts
        entries.append((side_rank.get(side, 9), metric_rank.get(metric, 9),
                        _natural_key(label), label, side, metric, val))
    entries.sort(key=lambda e: (e[0], e[1], e[2]))

    res_rows, par_rows = [], []
    for _, _, _, label, side, metric, val in entries:
        mshort = METRIC_SHORT.get(metric, metric)
        if val.get("status") != "OK":
            res_rows.append([label, side.capitalize(), mshort,
                             "FAILED", "FAILED", "FAILED", "-", "-", "-"])
            continue
        oi = val.get("optuna_info") or {}
        om = val.get("metrics") or {}
        bm = oi.get("baseline_metrics") or {}
        hm = oi.get("holdout_metrics") or {}
        guard = bool(oi.get("regression_guard_triggered"))
        res_rows.append([
            label, side.capitalize(), mshort,
            _fmt_money0(bm.get("total_pnl")), _fmt_money0(om.get("total_pnl")),
            _fmt_money0(hm.get("total_pnl")),
            f"{_fmt_pf(bm.get('profit_factor'))} -> "
            f"{_fmt_pf(om.get('profit_factor'))}",
            f"{bm.get('trade_count', '-')} / {om.get('trade_count', '-')}"
            f" / {hm.get('trade_count', '-')}",
            _fmt_blocks(oi),
        ])
        # Guarded individual rows render '-' params (the trial was DISCARDED
        # — same semantics as the machine report / report tests).
        sp = {} if guard else (oi.get("params") or {})
        if not sp and not guard and "all_trial_params" in oi:
            suffix = f"_{side}"
            sp = {k[: -len(suffix)]: v
                  for k, v in oi["all_trial_params"].items()
                  if k.endswith(suffix)}
        par_rows.append(
            [label, side.capitalize(), mshort] + _param_cells(sp)
            + [_fmt_best_trial_readable(oi)])

    lines += ["### Individual sides — optimized params (pass-1)", ""]
    lines += _render_md_table(
        ["Experiment", "Side", "Metric", "PnL (pre)", "PnL (opt)",
         "PnL (holdout)", "PF (pre->opt)", "Trades (pre/opt/ho)",
         "Block Sharpes"],
        res_rows, right_align={3, 4, 5, 7})
    lines += ["", "### Individual per-side optimized parameters", ""]
    lines += _render_md_table(
        ["Experiment", "Side", "Metric", "Thr", "TP", "SL", "Trail",
         "TrailOff", "Cooldown", "MaxHold", "Consec", "ATR", "Best Trial"],
        par_rows, right_align={3, 4, 5, 6, 7, 8, 9, 10, 11})
    lines += [""]

    out_path = os.path.join(
        batch_dir, f"batch_summary_optimized_{objective}_readable.md")
    _write_text(out_path, "\n".join(lines))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Generate pass-1 baseline-graft ensemble artifacts for a "
                    "batch run (strictly additive; the default pipeline "
                    "product since pass-2 became opt-in).")
    parser.add_argument("--batch-dir", required=True,
                        help="Batch run directory (reports/batch_runs/...)")
    parser.add_argument("--objectives", default="sharpe",
                        help="Comma-separated objectives (default: sharpe)")
    parser.add_argument("--data", default=None,
                        help="OHLCV parquet override (default: manifest "
                             "defaults.local_data_path, then the "
                             "manifest-derived data/processed path)")
    parser.add_argument("--exec-data", default=None,
                        help="Raw exec parquet override (default: manifest "
                             "resolution; embedded EXEC_* fallback)")
    parser.add_argument("--slippage-per-side", type=float, default=None,
                        help="Slippage override (default: manifest "
                             "baseline.execution_workflow.slippage_per_side)")
    parser.add_argument("--render-optimized", action="store_true",
                        help="Render batch_summary_optimized_<obj>_readable.md "
                             "from the optimization_results JSONs instead of "
                             "generating baseline artifacts (strictly additive; "
                             "the machine-contract summaries are never modified)")
    args = parser.parse_args(argv)

    batch_dir = args.batch_dir
    if not os.path.isdir(batch_dir):
        raise ValueError(f"--batch-dir does not exist: {batch_dir}")

    manifest_path = os.path.join(batch_dir, "manifest.json")
    if not os.path.isfile(manifest_path):
        raise ValueError(f"manifest.json not found in batch dir: {manifest_path}")
    manifest = _read_json(manifest_path)

    objectives = [o.strip() for o in args.objectives.split(",") if o.strip()]
    if not objectives:
        raise ValueError("--objectives resolved to an empty list")

    if args.render_optimized:
        for objective in objectives:
            _run_readable_optimized(batch_dir, objective)
        return

    symbol = _resolve_symbol(manifest, batch_dir)
    get_instrument(symbol)  # fail-fast on unknown instruments
    slippage = _resolve_slippage(args.slippage_per_side, manifest, batch_dir)
    contract_multiplier = dollars_per_point(symbol)
    print(f"  [BASELINE] economics: symbol={symbol} "
          f"contract_multiplier={contract_multiplier} slippage={slippage}")

    for objective in objectives:
        _run_objective(batch_dir, objective, args, manifest, symbol,
                       slippage, contract_multiplier)


if __name__ == "__main__":
    main()
