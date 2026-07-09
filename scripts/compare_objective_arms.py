#!/usr/bin/env python3
"""
compare_objective_arms.py — the objective A/B readout.

Reads the per-arm post-optimizer artifacts from ONE batch directory
(ticket block-sharpe-objective-ab_07092026_1031):

    batch_summary_optimized_<arm>.md            (pass-1, per-side individual)
    batch_summary_optimized_ensembles_<arm>.md  (pass-2, ensembles)

for every arm present in {sharpe, sortino, block_min, block_median,
block_mean_std}, and writes `objective_ab_summary.md` into the batch dir:

  * a header stating each arm's block params (parsed from the self-describing
    report headers stamped by batch_post_optimizer);
  * per-side individual tables and an ensemble table — rows = experiment /
    ensemble label, columns per arm = PnL (holdout) [leading/emphasized],
    PnL (opt), Trades (holdout).

EXPECTED PATTERN (documented in the report itself): block arms' OPT PnL is
lower than the sharpe baseline's BY CONSTRUCTION — the block objective trades
in-sample PnL for cross-block consistency. The A/B verdict is holdout PnL.

Missing files/arms are reported and tolerated (partial tables, exit 0);
exit 1 only when NO arm artifacts are found at all. Pure stdlib (no pandas)
so it runs anywhere — local, VM, any conda env.

Usage:
  python scripts/compare_objective_arms.py --batch-dir reports/batch_runs/batch_<ts>
"""
import argparse
import os
import re
import sys
from datetime import datetime

# Arm order is display order: the sharpe baseline leads.
VALID_ARMS = ("sharpe", "sortino", "block_min", "block_median", "block_mean_std")
BLOCK_ARMS = {"block_min", "block_median", "block_mean_std"}

# Self-describing header lines stamped by batch_post_optimizer (phase 1 of
# ticket block-sharpe-objective-ab_07092026_1031). Absent on pre-ticket runs.
HEADER_KEYS = (
    "objective_metric",
    "n_blocks",
    "lambda_dispersion",
    "min_block_months",
    "holdout_months",
)

_DETAIL_RE = re.compile(r"^### (.+)\|(long|short)\|(logloss|average_precision)\s*$")
_HOLDOUT_RE = re.compile(r"^- \*\*Holdout\*\*: .*\(Total:\s*(\S+)\)")


def _read_lines(path):
    with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
        return [line.rstrip("\n") for line in f]


def _parse_money(s):
    """'$-56,740' -> -56740.0; '-' / unparseable -> None."""
    s = (s or "").strip().replace("$", "").replace(",", "").replace("**", "")
    if not s or s == "-":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _parse_trades_total(s):
    """'146/84/62' -> 146 (T of T/L/S); plain int passes through; else None."""
    s = (s or "").strip()
    if not s or s == "-":
        return None
    head = s.split("/")[0].strip()
    try:
        return int(head)
    except ValueError:
        return None


def _parse_header_params(lines):
    """Parse the self-describing `key: value` header lines (stop at the first
    section heading — headers precede all tables)."""
    params = {}
    for line in lines:
        if line.startswith("### "):
            break
        m = re.match(r"^(%s):\s*(.+?)\s*$" % "|".join(HEADER_KEYS), line)
        if m:
            params[m.group(1)] = m.group(2)
    return params


def parse_individual_report(path):
    """Parse batch_summary_optimized_<arm>.md.

    Returns (header_params, rows) with rows keyed (side, metric, experiment)
    -> {pnl_opt, pnl_holdout, trades_holdout}. Table-walking idiom mirrors
    agent/unified_pair_optimizer.parse_markdown_table (same column indices:
    0=Experiment, 6=PnL (opt), 7=PnL (holdout)); holdout trades come from the
    "Optimized Parameters Detail" sections (the per-side tables carry no
    holdout-trade column).
    """
    lines = _read_lines(path)
    params = _parse_header_params(lines)

    # Holdout trade totals from detail sections: "### <label>|<side>|<metric>"
    holdout_trades = {}
    detail_key = None
    for raw in lines:
        line = raw.strip()
        m = _DETAIL_RE.match(line)
        if m:
            detail_key = (m.group(2), m.group(3), m.group(1).strip())
            continue
        if line.startswith("### "):
            detail_key = None
            continue
        if detail_key:
            hm = _HOLDOUT_RE.match(line)
            if hm:
                try:
                    holdout_trades[detail_key] = int(hm.group(1))
                except ValueError:
                    pass

    rows = {}
    in_table = False
    side = metric = None
    current_section = None
    for raw in lines:
        line = raw.strip()
        if line.startswith("### "):
            current_section = line
            in_table = False
            continue
        if line.startswith("|") and "Experiment" in line:
            in_table = False
            if not current_section:
                continue
            is_long = "Long" in current_section
            is_short = "Short" in current_section
            is_logloss = "Logloss" in current_section
            is_ap = "Average Precision" in current_section
            if (is_long or is_short) and (is_logloss or is_ap):
                side = "long" if is_long else "short"
                metric = "logloss" if is_logloss else "average_precision"
                in_table = True
            continue
        if line.startswith("|") and line.replace(" ", "").replace("-", "").strip("|") == "":
            continue  # separator row
        if line.startswith("|") and in_table:
            cols = [c.strip() for c in line.split("|")][1:-1]
            if len(cols) < 8:
                continue
            exp = cols[0]
            rows[(side, metric, exp)] = {
                "pnl_opt": _parse_money(cols[6]),
                "pnl_holdout": _parse_money(cols[7]),
                "trades_holdout": holdout_trades.get((side, metric, exp)),
            }
        else:
            in_table = False
    return params, rows


def parse_ensemble_report(path):
    """Parse batch_summary_optimized_ensembles_<arm>.md.

    Returns (header_params, rows) with rows keyed by ensemble label
    ("<Long Model> + <Short Model>") -> {pnl_opt, pnl_holdout, trades_holdout}.
    Ensemble table columns (stable leading indices, with or without the Block
    Sharpes column): 1=Experiment, 2=Long Model, 3=Short Model,
    6=Trades (ho) T/L/S, 10=PnL (opt), 11=PnL (holdout).
    """
    lines = _read_lines(path)
    params = _parse_header_params(lines)

    rows = {}
    in_table = False
    current_section = None
    for raw in lines:
        line = raw.strip()
        if line.startswith("### "):
            current_section = line
            in_table = False
            continue
        if line.startswith("|") and "Experiment" in line:
            in_table = bool(current_section and "Ensembles" in current_section)
            continue
        if line.startswith("|") and line.replace(" ", "").replace("-", "").strip("|") == "":
            continue  # separator row
        if line.startswith("|") and in_table:
            cols = [c.strip() for c in line.split("|")][1:-1]
            if len(cols) < 12:
                continue
            label = f"{cols[2]} + {cols[3]}"
            rows[label] = {
                "pnl_opt": _parse_money(cols[10]),
                "pnl_holdout": _parse_money(cols[11]),
                "trades_holdout": _parse_trades_total(cols[6]),
            }
        else:
            in_table = False
    return params, rows


def _fmt_money(v):
    return f"${v:,.0f}" if v is not None else "-"


def _fmt_int(v):
    return str(v) if v is not None else "-"


def _arm_columns_header(arms):
    """Per-arm column triplet; PnL (holdout) leads and is emphasized."""
    heads = []
    for arm in arms:
        heads += [
            f"**{arm} PnL (holdout)**",
            f"{arm} PnL (opt)",
            f"{arm} Trades (holdout)",
        ]
    return heads


def _comparison_table(row_label_header, row_keys, per_arm_rows, arms):
    """Markdown comparison table: one row per key, 3 columns per arm."""
    out = []
    header = [row_label_header] + _arm_columns_header(arms)
    out.append("| " + " | ".join(header) + " |")
    out.append("|" + "---|" * len(header))
    for key, display in row_keys:
        cells = [display]
        for arm in arms:
            rec = per_arm_rows.get(arm, {}).get(key)
            if rec is None:
                cells += ["-", "-", "-"]
            else:
                pnl_ho = rec.get("pnl_holdout")
                cells += [
                    f"**{_fmt_money(pnl_ho)}**" if pnl_ho is not None else "-",
                    _fmt_money(rec.get("pnl_opt")),
                    _fmt_int(rec.get("trades_holdout")),
                ]
        out.append("| " + " | ".join(cells) + " |")
    return out


def main():
    ap = argparse.ArgumentParser(description="Cross-arm objective A/B summary for one batch dir")
    ap.add_argument("--batch-dir", required=True, help="reports/batch_runs/<batch_id> directory")
    ap.add_argument(
        "--output", default=None,
        help="Output markdown path (default: <batch-dir>/objective_ab_summary.md)",
    )
    args = ap.parse_args()

    batch_dir = args.batch_dir
    if not os.path.isdir(batch_dir):
        print(f"ERROR: batch dir not found: {batch_dir}")
        sys.exit(1)

    # ── Discover arms from whichever per-arm artifacts exist ────────────────
    arm_files = {}   # arm -> {"individual": path|None, "ensembles": path|None}
    missing = []
    for arm in VALID_ARMS:
        ind = os.path.join(batch_dir, f"batch_summary_optimized_{arm}.md")
        ens = os.path.join(batch_dir, f"batch_summary_optimized_ensembles_{arm}.md")
        ind_ok, ens_ok = os.path.isfile(ind), os.path.isfile(ens)
        if ind_ok or ens_ok:
            arm_files[arm] = {
                "individual": ind if ind_ok else None,
                "ensembles": ens if ens_ok else None,
            }
            if not ind_ok:
                missing.append(f"{arm}: batch_summary_optimized_{arm}.md absent")
            if not ens_ok:
                missing.append(f"{arm}: batch_summary_optimized_ensembles_{arm}.md absent")

    if not arm_files:
        print(f"ERROR: no per-arm artifacts (batch_summary_optimized_<arm>.md / "
              f"..._ensembles_<arm>.md) found in {batch_dir} for arms {VALID_ARMS}")
        sys.exit(1)

    arms = [a for a in VALID_ARMS if a in arm_files]
    print(f"Arms detected: {', '.join(arms)}")
    for note in missing:
        print(f"  [absent] {note}")

    # ── Parse per-arm artifacts ─────────────────────────────────────────────
    arm_params = {}
    ind_rows = {}   # arm -> {(side, metric, exp): rec}
    ens_rows = {}   # arm -> {label: rec}
    for arm in arms:
        params = {}
        if arm_files[arm]["individual"]:
            p, rows = parse_individual_report(arm_files[arm]["individual"])
            params.update(p)
            ind_rows[arm] = rows
        if arm_files[arm]["ensembles"]:
            p, rows = parse_ensemble_report(arm_files[arm]["ensembles"])
            for k, v in p.items():
                params.setdefault(k, v)
            ens_rows[arm] = rows
        arm_params[arm] = params

    # ── Build the report ────────────────────────────────────────────────────
    lines = []
    lines.append(f"# Objective A/B Summary — {os.path.basename(os.path.abspath(batch_dir))}")
    lines.append("")
    lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"Arms compared: {', '.join(arms)}")
    lines.append("")
    lines.append("> **Read the verdict on PnL (holdout) — the leading, emphasized column.**")
    lines.append("> Block arms' opt (in-sample) PnL is EXPECTED to be lower than the sharpe")
    lines.append("> baseline's by construction: the block-wise objective sacrifices in-sample")
    lines.append("> PnL for cross-block consistency. Opt PnL is shown for diagnosis only.")
    lines.append("")
    lines.append("## Arm parameters (from report headers)")
    lines.append("")
    lines.append("| Arm | objective_metric | n_blocks | lambda_dispersion | min_block_months | holdout_months | Pass-1 report | Ensemble report |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for arm in arms:
        p = arm_params[arm]
        stamped = any(k in p for k in HEADER_KEYS)
        def g(key, _p=p, _stamped=stamped):
            if key in _p:
                return _p[key]
            return "(not stamped)" if not _stamped else "-"
        lines.append(
            f"| {arm} | {g('objective_metric')} | {g('n_blocks')} | {g('lambda_dispersion')} | "
            f"{g('min_block_months')} | {g('holdout_months')} | "
            f"{'yes' if arm_files[arm]['individual'] else 'ABSENT'} | "
            f"{'yes' if arm_files[arm]['ensembles'] else 'ABSENT'} |"
        )
    lines.append("")
    if missing:
        lines.append("## Missing artifacts")
        lines.append("")
        for note in missing:
            lines.append(f"- {note}")
        lines.append("")

    # Per-side individual tables (rows = experiment, LL/AP disambiguated)
    metric_tag = {"logloss": "LL", "average_precision": "AP"}
    for side in ("long", "short"):
        lines.append(f"## Individual — {side.capitalize()}")
        lines.append("")
        keys = set()
        for arm in arms:
            for (s, m, exp) in ind_rows.get(arm, {}):
                if s == side:
                    keys.add((m, exp))
        if not keys:
            lines.append(f"_No {side}-side individual rows found in any arm._")
            lines.append("")
            continue
        row_keys = [
            ((side, m, exp), f"{exp} ({metric_tag[m]})")
            for (m, exp) in sorted(keys, key=lambda t: (t[1], t[0]))
        ]
        lines += _comparison_table("Experiment", row_keys, ind_rows, arms)
        lines.append("")

    # Ensemble table (rows = ensemble label; arms may pick DIFFERENT pairs —
    # per-arm pair selection is isolated by design, so rows are a union).
    lines.append("## Ensembles")
    lines.append("")
    ens_keys = set()
    for arm in arms:
        ens_keys.update(ens_rows.get(arm, {}).keys())
    if not ens_keys:
        lines.append("_No ensemble rows found in any arm._")
        lines.append("")
    else:
        row_keys = [(label, label) for label in sorted(ens_keys)]
        lines += _comparison_table("Ensemble", row_keys, ens_rows, arms)
        lines.append("")
        lines.append("_Rows are the union across arms; a `-` means that arm's pair selection_")
        lines.append("_did not pick this ensemble (per-arm selection is isolated by design)._")
        lines.append("")

    out_path = args.output or os.path.join(batch_dir, "objective_ab_summary.md")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"Wrote: {out_path}")
    sys.exit(0)


if __name__ == "__main__":
    main()
