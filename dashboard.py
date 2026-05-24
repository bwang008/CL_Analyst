"""CL Analyst — Model Registry & Backtest Dashboard
===================================================
Launch:  streamlit run dashboard.py
"""
from __future__ import annotations
from pathlib import Path
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from dashboard_data import (
    DEFAULT_BATCH_ROOT,
    DEFAULT_REGISTRY_ROOT,
    scan_batch_runs,
    load_optimization_results,
    load_batch_progress,
    load_batch_manifest,
    parse_ensemble_backtest,
    scan_model_registry,
    _find_experiment_dir,
)

# ── Page config ──────────────────────────────────────────────
st.set_page_config(
    page_title="CL Analyst — Backtest Dashboard",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Custom CSS ───────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
html, body, [class*="st-"] { font-family: 'Inter', sans-serif; }
/* KPI cards */
div[data-testid="stMetric"] {
    background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
    border: 1px solid #0f3460;
    border-radius: 12px;
    padding: 16px 20px;
    box-shadow: 0 4px 16px rgba(0,0,0,.25);
}
div[data-testid="stMetric"] label { color: #a8b2d1 !important; font-size: 0.78rem !important; }
div[data-testid="stMetric"] [data-testid="stMetricValue"] { color: #ccd6f6 !important; font-weight: 600 !important; }
/* Positive / Negative deltas */
.pnl-pos { color: #64ffda; font-weight: 600; }
.pnl-neg { color: #ff6b6b; font-weight: 600; }
/* Section dividers */
.section-header {
    background: linear-gradient(90deg, #0f3460, #1a1a2e);
    padding: 10px 18px;
    border-radius: 8px;
    margin: 24px 0 12px 0;
    font-size: 1.15rem;
    font-weight: 600;
    color: #ccd6f6;
    letter-spacing: 0.03em;
}
/* Status badges */
.badge-ok { background:#064e3b; color:#6ee7b7; padding:2px 10px; border-radius:6px; font-size:.8rem; }
.badge-fail { background:#7f1d1d; color:#fca5a5; padding:2px 10px; border-radius:6px; font-size:.8rem; }
/* Tabs styling */
button[data-baseweb="tab"] { font-weight: 500 !important; }
</style>
""", unsafe_allow_html=True)


# ═══════════════════════════════════════════════════════════════
#  HELPER UTILITIES
# ═══════════════════════════════════════════════════════════════

def _fmt_dollar(val: float) -> str:
    if val >= 0:
        return f'<span class="pnl-pos">${val:,.0f}</span>'
    return f'<span class="pnl-neg">-${abs(val):,.0f}</span>'


def _pnl_color(val: float) -> str:
    return "color: #64ffda" if val >= 0 else "color: #ff6b6b"


def _style_leaderboard(df: pd.DataFrame) -> pd.io.formats.style.Styler:
    """Apply conditional formatting to the leaderboard."""
    pnl_cols = [c for c in df.columns if "pnl" in c.lower()]
    pf_cols = [c for c in df.columns if "pf" in c.lower() or "profit" in c.lower()]

    def color_pnl(v):
        try:
            v = float(v)
            return "color: #64ffda" if v >= 0 else "color: #ff6b6b"
        except (ValueError, TypeError):
            return ""

    def color_pf(v):
        try:
            v = float(v)
            return "color: #64ffda" if v >= 1.0 else "color: #ff6b6b"
        except (ValueError, TypeError):
            return ""

    styler = df.style
    for c in pnl_cols:
        if c in df.columns:
            styler = styler.map(color_pnl, subset=[c])
    for c in pf_cols:
        if c in df.columns:
            styler = styler.map(color_pf, subset=[c])
    return styler.format({c: "${:,.0f}" for c in pnl_cols if c in df.columns}, na_rep="—")


# ═══════════════════════════════════════════════════════════════
#  SIDEBAR
# ═══════════════════════════════════════════════════════════════

def render_sidebar() -> tuple[str, str, str, str]:
    st.sidebar.markdown("## 📊 Dashboard Controls")
    batch_root = st.sidebar.text_input("Batch Root Directory", value=DEFAULT_BATCH_ROOT)
    batches = scan_batch_runs(batch_root)
    if not batches:
        st.sidebar.warning("No batch folders found.")
        st.stop()

    selected_batch = st.sidebar.selectbox("Select Batch", batches, index=0)
    objective = st.sidebar.radio("Optimization Objective", ["sharpe", "sortino"], horizontal=True)
    side_filter = st.sidebar.radio("Side Filter", ["Both", "Long", "Short"], horizontal=True)
    batch_dir = str(Path(batch_root) / selected_batch)

    # Sidebar batch meta
    progress = load_batch_progress(batch_dir)
    if progress:
        total = progress.get("total", 0)
        completed = progress.get("completed", 0)
        failed = progress.get("failed", 0)
        st.sidebar.markdown("---")
        st.sidebar.caption(f"**Started:** {progress.get('started_at', 'N/A')}")
        st.sidebar.caption(f"**Completed:** {progress.get('completed_at', 'N/A')}")
        st.sidebar.caption(f"**Experiments:** {completed}/{total} ✅  {failed} ❌")
    return batch_dir, objective, side_filter, batch_root


# ═══════════════════════════════════════════════════════════════
#  SECTION 1 — BATCH OVERVIEW
# ═══════════════════════════════════════════════════════════════

def render_batch_overview(df: pd.DataFrame, progress: dict, side_filter: str,
                          batch_dir: str) -> str | None:
    st.markdown('<div class="section-header">📋 Section 1 — Batch Overview</div>', unsafe_allow_html=True)

    if df.empty:
        st.warning("No optimization results found for this batch / objective.")
        return None

    # Apply side filter
    filtered = df.copy()
    if side_filter == "Long":
        filtered = filtered[filtered["side"] == "long"]
    elif side_filter == "Short":
        filtered = filtered[filtered["side"] == "short"]

    if filtered.empty:
        st.info("No results match the current side filter.")
        return None

    # ── KPI Row ──
    k1, k2, k3, k4, k5 = st.columns(5)
    k1.metric("Total Experiments", len(filtered))
    ok_count = len(filtered[filtered["status"] == "OK"])
    k2.metric("Successful", ok_count)
    k3.metric("Best Opt PnL", f"${filtered['opt_pnl'].max():,.0f}")
    k4.metric("Best Holdout PnL", f"${filtered['holdout_pnl'].max():,.0f}")

    # Sanity check: pre-opt signal count
    zero_signal = filtered[filtered["pre_trades"] == 0]
    if len(zero_signal) > 0:
        k5.metric("⚠️ Zero Pre-Trades", len(zero_signal))
    else:
        k5.metric("Pre-Trade Sanity", "✅ All OK")

    # ── Manifest / Batch Config ──
    manifest = load_batch_manifest(batch_dir)
    if manifest:
        with st.expander("📄 Batch Config (manifest.json)", expanded=False):
            defaults = manifest.get("defaults", {})
            experiments = manifest.get("experiments", [])
            comment = manifest.get("_comment", "")

            if comment:
                st.caption(f"💬 *{comment}*")

            # Key training / infra settings in metric cards
            mc1, mc2, mc3, mc4, mc5 = st.columns(5)
            mc1.metric("Optuna Trials", defaults.get("n_trials", "—"))
            mc2.metric("Post-Opt Trials", defaults.get("post_optimizer_trials", "—"))
            mc3.metric("Machine Type", defaults.get("machine_type", "—"))
            mc4.metric("Max Folds", defaults.get("max_folds", "—"))
            mc5.metric("Timeout (min)", defaults.get("timeout_minutes", "—"))

            mc6, mc7, mc8, mc9 = st.columns(4)
            mc6.metric("Max Concurrent VMs", defaults.get("max_concurrent_vms", "—"))
            mc7.metric("vCPUs / VM", defaults.get("vcpus_per_vm", "—"))
            mc8.metric("Max vCPU Budget", defaults.get("max_concurrent_vcpus", "—"))
            mc9.metric("Holdout Months", defaults.get("post_optimizer_holdout_months", "—"))

            # Hyperparameter search bounds
            st.markdown("##### Hyperparameter Search Bounds")
            bounds_data = {
                "Parameter": [
                    "max_depth", "num_leaves", "learning_rate",
                    "min_child_samples", "feature_fraction", "n_estimators (max)",
                    "early_stopping_rounds",
                ],
                "Min": [
                    defaults.get("max_depth_min", "—"),
                    defaults.get("num_leaves_min", "—"),
                    defaults.get("learning_rate_min", "—"),
                    defaults.get("min_child_samples_min", "—"),
                    defaults.get("feature_fraction_min", "—"),
                    "—",
                    "—",
                ],
                "Max": [
                    defaults.get("max_depth_max", "—"),
                    defaults.get("num_leaves_max", "—"),
                    defaults.get("learning_rate_max", "—"),
                    defaults.get("min_child_samples_max", "—"),
                    defaults.get("feature_fraction_max", "—"),
                    defaults.get("max_n_estimators", "—"),
                    defaults.get("early_stopping_rounds", "—"),
                ],
            }
            st.dataframe(pd.DataFrame(bounds_data), use_container_width=True, hide_index=True)

            # Experiment target list
            if experiments:
                st.markdown("##### Experiment Targets")
                exp_rows = []
                for exp in experiments:
                    exp_rows.append({
                        "Label": exp.get("label", "—"),
                        "Target Long": exp.get("target_long", "—"),
                        "Target Short": exp.get("target_short", "—"),
                        "GCS Prefix": exp.get("gcs_prefix", "—"),
                    })
                st.dataframe(pd.DataFrame(exp_rows), use_container_width=True, hide_index=True)

            # Strategy config & data path
            st.markdown("##### Data & Strategy")
            st.code(f"Strategy Config: {defaults.get('strategy_config', 'N/A')}\n"
                    f"GCS Data Path:   {defaults.get('gcs_data_path', 'N/A')}\n"
                    f"Metrics:         {defaults.get('metrics', 'N/A')}\n"
                    f"Provisioning:    {defaults.get('provisioning_model', 'N/A')}",
                    language=None)

    # ── Leaderboard Table ──
    st.markdown("#### Leaderboard")
    display_cols = [
        "experiment", "side", "ml_metric",
        "pre_trades", "pre_pf", "pre_pnl",
        "opt_trades", "opt_pf", "opt_pnl",
        "holdout_pnl", "holdout_trades",
        "consistency", "opt_sharpe",
    ]
    show = filtered[[c for c in display_cols if c in filtered.columns]].copy()
    show.columns = [
        "Experiment", "Side", "ML Metric",
        "Pre Trades", "Pre PF", "Pre PnL",
        "Opt Trades", "Opt PF", "Opt PnL",
        "Holdout PnL", "Holdout Trades",
        "Consistency", "Sharpe",
    ][:len(show.columns)]

    st.dataframe(
        _style_leaderboard(show),
        use_container_width=True,
        height=min(400, 38 * len(show) + 40),
    )

    # ── Drill-down selector ──
    options = filtered["key"].tolist()
    selected_key = st.selectbox("🔍 Select experiment for drill-down", options, index=0)
    return selected_key


# ═══════════════════════════════════════════════════════════════
#  SECTION 2 — EXPERIMENT DRILL-DOWN
# ═══════════════════════════════════════════════════════════════

def render_drilldown(df: pd.DataFrame, selected_key: str, progress: dict):
    st.markdown('<div class="section-header">🔬 Section 2 — Experiment Drill-Down</div>', unsafe_allow_html=True)

    row = df[df["key"] == selected_key].iloc[0]
    experiment_label = row["experiment"]
    ml_metric = row["ml_metric"]

    tab1, tab2, tab3, tab4 = st.tabs([
        "📈 Optimization Detail",
        "📊 Charts",
        "⚙️ Hyperparameters",
        "🔧 Execution & Errors",
    ])

    # ── Tab 1: Optimization Detail ──
    with tab1:
        st.markdown(f"### {experiment_label} · {row['side'].upper()} · {ml_metric}")

        # Before / After cards
        col_pre, col_opt, col_hold = st.columns(3)
        with col_pre:
            st.markdown("**🔹 Pre-Optimization (Baseline)**")
            st.metric("Trades", f"{row['pre_trades']:,}")
            st.metric("Profit Factor", f"{row['pre_pf']:.4f}")
            st.metric("PnL", f"${row['pre_pnl']:,.2f}")
            st.metric("Sharpe", f"{row['pre_sharpe']:.4f}")
            st.metric("Max Drawdown", f"${row['pre_max_dd']:,.2f}")

        with col_opt:
            st.markdown("**🔸 Post-Optimization**")
            st.metric("Trades", f"{row['opt_trades']:,}",
                      delta=f"{row['opt_trades'] - row['pre_trades']:+,}")
            st.metric("Profit Factor", f"{row['opt_pf']:.4f}",
                      delta=f"{row['opt_pf'] - row['pre_pf']:+.4f}")
            st.metric("PnL", f"${row['opt_pnl']:,.2f}",
                      delta=f"${row['opt_pnl'] - row['pre_pnl']:+,.0f}")
            st.metric("Sharpe", f"{row['opt_sharpe']:.4f}")
            st.metric("Max Drawdown", f"${row['opt_max_dd']:,.2f}")

        with col_hold:
            holdout_ok = row["holdout_pnl"] > 0
            badge = "badge-ok" if holdout_ok else "badge-fail"
            verdict = "PASS" if holdout_ok else "FAIL"
            st.markdown(f'**🔶 Holdout ({row["holdout_months"]}mo)** '
                        f'<span class="{badge}">{verdict}</span>', unsafe_allow_html=True)
            st.metric("Trades", f"{row['holdout_trades']:,}")
            st.metric("Profit Factor", f"{row['holdout_pf']:.4f}")
            st.metric("PnL", f"${row['holdout_pnl']:,.2f}")
            st.metric("Sharpe", f"{row['holdout_sharpe']:.4f}")
            st.metric("Max Drawdown", f"${row['holdout_max_dd']:,.2f}")

        # Optimized strategy parameters
        st.markdown("---")
        st.markdown("#### Optimized Strategy Parameters")
        params = row.get("params", {})
        if params:
            pcols = st.columns(min(4, len(params)))
            for i, (k, v) in enumerate(params.items()):
                pcols[i % len(pcols)].metric(k, f"{v}")
        else:
            st.info("No strategy parameters recorded.")

        # Consistency score
        st.metric("Consistency Score", f"{row['consistency']:.4f}")

    # ── Tab 2: Charts ──
    with tab2:
        _render_charts(row, experiment_label, ml_metric, progress)

    # ── Tab 3: Hyperparameters ──
    with tab3:
        st.markdown(f"### Best Trial: #{row['trial_number']} / {row['n_trials']}")
        st.markdown(f"**Wall time:** {row['wall_time_s']:.1f}s")
        params = row.get("params", {})
        if params:
            st.json(params)
        else:
            st.info("No hyperparameter data available.")

        st.markdown("#### Full Row Data (JSON)")
        safe_row = {k: v for k, v in row.to_dict().items() if k != "params"}
        safe_row["params"] = params
        st.json(safe_row)

    # ── Tab 4: Execution & Errors ──
    with tab4:
        _render_execution(experiment_label, progress)


def _render_charts(row, experiment_label: str, ml_metric: str, progress: dict):
    """Render equity curve, drawdown, and exit distribution charts."""
    # Try to find and parse ensemble backtest
    exp_dir = _find_experiment_dir(progress, experiment_label)
    ec_df = pd.DataFrame()
    if exp_dir:
        # Try canary_output path first, then direct path
        for sub in [
            f"registry/canary_output/ensemble_backtest_{ml_metric}.txt",
            f"ensemble_backtest_{ml_metric}.txt",
        ]:
            fp = Path(exp_dir) / sub
            if fp.is_file():
                ec_df = parse_ensemble_backtest(str(fp))
                break

    if not ec_df.empty:
        # Cumulative PnL equity curve
        fig_eq = go.Figure()
        fig_eq.add_trace(go.Scatter(
            x=ec_df["month"], y=ec_df["cumulative_pnl"],
            mode="lines+markers",
            line=dict(color="#64ffda", width=2.5),
            marker=dict(size=5),
            name="Cumulative PnL",
            hovertemplate="<b>%{x|%Y-%m}</b><br>PnL: $%{y:,.0f}<extra></extra>",
        ))
        fig_eq.update_layout(
            title=f"Ensemble Equity Curve — {experiment_label} ({ml_metric})",
            template="plotly_dark",
            paper_bgcolor="#0e1117", plot_bgcolor="#0e1117",
            xaxis_title="Month", yaxis_title="Cumulative PnL ($)",
            font=dict(family="Inter", color="#ccd6f6"),
            height=400,
        )
        # Zero line
        fig_eq.add_hline(y=0, line_dash="dash", line_color="#4a5568", opacity=0.6)
        st.plotly_chart(fig_eq, use_container_width=True)

        # Drawdown chart
        fig_dd = go.Figure()
        fig_dd.add_trace(go.Scatter(
            x=ec_df["month"], y=ec_df["drawdown"],
            fill="tozeroy",
            line=dict(color="#ff6b6b", width=1.5),
            fillcolor="rgba(255,107,107,0.15)",
            name="Drawdown",
        ))
        fig_dd.update_layout(
            title="Drawdown",
            template="plotly_dark",
            paper_bgcolor="#0e1117", plot_bgcolor="#0e1117",
            xaxis_title="Month", yaxis_title="Drawdown ($)",
            font=dict(family="Inter", color="#ccd6f6"),
            height=280,
        )
        st.plotly_chart(fig_dd, use_container_width=True)

        # Monthly PnL bar chart
        colors = ["#64ffda" if v >= 0 else "#ff6b6b" for v in ec_df["net_pnl"]]
        fig_bar = go.Figure(go.Bar(
            x=ec_df["month"], y=ec_df["net_pnl"],
            marker_color=colors,
            hovertemplate="<b>%{x|%Y-%m}</b><br>$%{y:,.0f}<extra></extra>",
        ))
        fig_bar.update_layout(
            title="Monthly Net PnL",
            template="plotly_dark",
            paper_bgcolor="#0e1117", plot_bgcolor="#0e1117",
            font=dict(family="Inter", color="#ccd6f6"),
            height=280,
        )
        st.plotly_chart(fig_bar, use_container_width=True)
    else:
        st.warning("Equity curve data not available for this experiment. "
                    "The ensemble backtest text file was not found or could not be parsed.")

    # Exit distribution pie chart (always available from optimization results)
    st.markdown("#### Exit Distribution (Optimized)")
    exits = {
        "Take Profit": row["opt_pct_tp"],
        "Stop Loss": row["opt_pct_sl"],
        "Time Barrier": row["opt_pct_time"],
        "Trailing BE": row["opt_pct_trailing"],
    }
    exits = {k: v for k, v in exits.items() if v > 0}
    if exits:
        fig_pie = px.pie(
            names=list(exits.keys()),
            values=list(exits.values()),
            hole=0.45,
            color_discrete_sequence=["#64ffda", "#ff6b6b", "#ffd93d", "#6c63ff"],
        )
        fig_pie.update_layout(
            template="plotly_dark",
            paper_bgcolor="#0e1117",
            font=dict(family="Inter", color="#ccd6f6"),
            height=320,
        )
        fig_pie.update_traces(textinfo="percent+label")
        st.plotly_chart(fig_pie, use_container_width=True)
    else:
        st.info("No exit distribution data available.")


def _render_execution(experiment_label: str, progress: dict):
    """Tab 4: surface failure reasons, wall times, VM info."""
    exps = progress.get("experiments", [])
    match = [e for e in exps if e.get("label") == experiment_label]
    if not match:
        st.info("No execution metadata found for this experiment.")
        return

    entry = match[0]
    status = entry.get("status", "UNKNOWN")
    is_fail = status not in ("COMPLETED",)

    badge = "badge-fail" if is_fail else "badge-ok"
    st.markdown(f'**Status:** <span class="{badge}">{status}</span>', unsafe_allow_html=True)

    c1, c2, c3 = st.columns(3)
    c1.metric("Wall Time (min)", f"{entry.get('wall_time_min', 'N/A')}")
    c2.metric("VM Name", entry.get("vm_name", "N/A"))
    c3.metric("Exit Code", entry.get("exit_code", "N/A"))

    if entry.get("failure_reason"):
        st.error(f"**Failure Reason:** {entry['failure_reason']}")

    local_dir = entry.get("local_dir", "")
    if local_dir:
        st.caption(f"📁 Local artifacts: `{local_dir}`")
    gcs = entry.get("gcs_prefix", "")
    if gcs:
        st.caption(f"☁️ GCS prefix: `{gcs}`")

    # Show full entry as expandable JSON
    with st.expander("Raw execution metadata"):
        st.json(entry)


# ═══════════════════════════════════════════════════════════════
#  SECTION 3 — MODEL REGISTRY
# ═══════════════════════════════════════════════════════════════

def render_model_registry():
    st.markdown('<div class="section-header">🗄️ Section 3 — Model Registry Browser</div>', unsafe_allow_html=True)
    registry_df = scan_model_registry(DEFAULT_REGISTRY_ROOT)
    if registry_df.empty:
        st.info("No models found in the registry.")
        return

    st.markdown(f"**{len(registry_df)} models** registered")

    # Feature count sanity check
    display = registry_df[[
        "model_id", "strategy", "target", "feature_count",
        "feature_groups", "boosting", "num_leaves", "max_depth",
        "learning_rate", "n_estimators", "has_model_pkl", "has_predictions",
    ]].copy()
    display.columns = [
        "Model ID", "Strategy", "Target", "Features",
        "Feature Groups", "Boosting", "Leaves", "Depth",
        "LR", "Estimators", "Has PKL", "Has OOS",
    ]
    st.dataframe(display, use_container_width=True, height=min(400, 38 * len(display) + 40))

    # Expandable detail
    selected_model = st.selectbox("Inspect model config", registry_df["model_id"].tolist())
    if selected_model:
        model_row = registry_df[registry_df["model_id"] == selected_model].iloc[0]
        raw_cfg = model_row.get("_raw_config", {})
        if raw_cfg:
            st.json(raw_cfg)


# ═══════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════

def main():
    st.markdown("# 📊 CL Analyst — Model Registry & Backtest Dashboard")
    st.caption("Browse batch experiment results, optimization outcomes, and model artifacts.")

    batch_dir, objective, side_filter, _ = render_sidebar()

    # Load data for selected batch
    df = load_optimization_results(batch_dir, objective)
    progress = load_batch_progress(batch_dir)

    # Section 1
    selected_key = render_batch_overview(df, progress, side_filter, batch_dir)

    # Section 2
    if selected_key and not df.empty:
        render_drilldown(df, selected_key, progress)

    # Section 3
    st.markdown("---")
    render_model_registry()


if __name__ == "__main__":
    main()
