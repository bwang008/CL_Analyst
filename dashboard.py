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
    # Signal analysis loaders
    load_prediction_pair,
    compute_conflict_matrix,
    compute_autocorrelation,
    compute_run_length_stats,
    scan_prediction_files,
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
    border-radius: 10px;
    padding: 8px 12px;
    box-shadow: 0 4px 16px rgba(0,0,0,.25);
}
div[data-testid="stMetric"] label {
    color: #a8b2d1 !important;
    font-size: 0.72rem !important;
    white-space: normal !important;
    word-break: break-word !important;
    overflow: visible !important;
}
div[data-testid="stMetric"] [data-testid="stMetricValue"] {
    color: #ccd6f6 !important;
    font-weight: 600 !important;
    font-size: 1.25rem !important;
    white-space: normal !important;
    word-break: break-word !important;
    overflow: visible !important;
}
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
        "consistency", "opt_sharpe", "opt_sortino",
    ]
    show = filtered[[c for c in display_cols if c in filtered.columns]].copy()
    show.columns = [
        "Experiment", "Side", "ML Metric",
        "Pre Trades", "Pre PF", "Pre PnL",
        "Opt Trades", "Opt PF", "Opt PnL",
        "Holdout PnL", "Holdout Trades",
        "Consistency", "Sharpe", "Sortino",
    ][:len(show.columns)]

    st.dataframe(
        _style_leaderboard(show),
        use_container_width=True,
        height=min(400, 38 * len(show) + 40),
    )

    # ── Drill-down selector ──
    options = sorted(filtered["key"].tolist())
    selected_key = st.selectbox("🔍 Select experiment for drill-down", options, index=0)
    return selected_key


# ═══════════════════════════════════════════════════════════════
#  SECTION 2 — EXPERIMENT DRILL-DOWN
# ═══════════════════════════════════════════════════════════════

def _render_feature_importance(row, experiment_label: str, ml_metric: str, progress: dict):
    st.markdown(f"### 🧬 Feature Importance · {experiment_label} · {row['side'].upper()} · {ml_metric}")

    exp_dir = _find_experiment_dir(progress, experiment_label)
    if not exp_dir:
        st.warning("Experiment directory not found in execution progress.")
        return

    p = Path(exp_dir)
    candidates = list(p.rglob("feature_importance.csv"))

    fi_path = None
    if candidates:
        for c in candidates:
            parent_name = c.parent.name.lower()
            if row['side'].lower() in parent_name and ml_metric.lower() in parent_name:
                fi_path = c
                break
        if not fi_path:
            fi_path = candidates[0]

    if not fi_path or not fi_path.is_file():
        st.warning("Feature importance CSV not found for this experiment model.")
        return

    try:
        df_fi = pd.read_csv(fi_path)
    except Exception as e:
        st.error(f"Error reading feature importance file: {e}")
        return

    if df_fi.empty or "feature" not in df_fi.columns or "importance" not in df_fi.columns:
        st.warning("Feature importance CSV is empty or format is invalid.")
        return

    # Sort and clean
    df_fi = df_fi.sort_values(by="importance", ascending=False).reset_index(drop=True)

    total_feats = len(df_fi)
    active_feats = len(df_fi[df_fi["importance"] > 0])
    zero_feats = len(df_fi[df_fi["importance"] == 0])

    # Render KPI metrics
    c1, c2, c3 = st.columns(3)
    c1.metric("Total Features", f"{total_feats:,}")
    c2.metric("Active Features (Gain > 0)", f"{active_feats:,}")
    c3.metric("Unused Features (Gain = 0)", f"{zero_feats:,}")

    # Top 15 horizontal bar chart
    top_15 = df_fi.head(15).copy()
    # Reverse for plotting so highest is at the top
    top_15_plot = top_15.iloc[::-1]

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=top_15_plot["importance"],
        y=top_15_plot["feature"],
        orientation="h",
        marker=dict(
            color=top_15_plot["importance"],
            colorscale=[[0, "#0f3460"], [1, "#64ffda"]],
        ),
        hovertemplate="<b>%{y}</b><br>Gain Importance: %{x:,.2f}<extra></extra>",
    ))
    fig.update_layout(
        title=f"Top 15 Most Influential Features (LGBM Gain)",
        template="plotly_dark",
        paper_bgcolor="#0e1117", plot_bgcolor="#0e1117",
        xaxis_title="Total Gain / Importance",
        yaxis_title="Feature Name",
        font=dict(family="Inter", color="#ccd6f6"),
        height=450,
        margin=dict(l=150, r=20, t=50, b=50),
    )
    st.plotly_chart(fig, use_container_width=True)

    # Detailed side-by-side tables
    st.markdown("#### Feature Importance Rankings")
    t1, t2 = st.columns(2)
    with t1:
        st.markdown("**🏆 Top 15 Most Important Features**")
        st.dataframe(df_fi.head(15), use_container_width=True, hide_index=True)

    with t2:
        st.markdown("**📉 Bottom 15 Least Important Features**")
        st.dataframe(df_fi.tail(15), use_container_width=True, hide_index=True)

    # Entire list expander
    with st.expander("🔍 View All Features Ranking", expanded=False):
        st.dataframe(df_fi, use_container_width=True)


def render_drilldown(df: pd.DataFrame, selected_key: str, progress: dict):
    st.markdown('<div class="section-header">🔬 Section 2 — Experiment Drill-Down</div>', unsafe_allow_html=True)

    row = df[df["key"] == selected_key].iloc[0]
    experiment_label = row["experiment"]
    ml_metric = row["ml_metric"]

    tab1, tab2, tab3, tab4, tab5 = st.tabs([
        "📈 Optimization Detail",
        "📊 Charts",
        "🧬 Feature Importance",
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

    # ── Tab 3: Feature Importance ──
    with tab3:
        _render_feature_importance(row, experiment_label, ml_metric, progress)

    # ── Tab 4: Hyperparameters ──
    with tab4:
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

    # ── Tab 5: Execution & Errors ──
    with tab5:
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
#  SECTION 4 — SIGNAL ANALYSIS
# ═══════════════════════════════════════════════════════════════

def render_signal_analysis():
    """Section 4: Visualize long/short signal overlap, density, and autocorrelation."""
    st.markdown('<div class="section-header">🔬 Section 4 — Signal Analysis</div>', unsafe_allow_html=True)
    st.caption("Analyze long and short model prediction overlap, conflict frequency, and signal clustering.")

    # ── File selection ──
    pred_files = scan_prediction_files()
    if not pred_files:
        st.warning("No prediction files found in `data/predictions/`. Run a backtest with predictions first.")
        return

    # Auto-detect long vs short files
    long_candidates = [f for f in pred_files if "long" in f.lower()]
    short_candidates = [f for f in pred_files if "short" in f.lower()]

    col_l, col_r = st.columns(2)
    with col_l:
        long_path = st.selectbox(
            "📈 Long Predictions",
            pred_files,
            index=pred_files.index(long_candidates[0]) if long_candidates else 0,
            key="sa_long",
        )
    with col_r:
        short_path = st.selectbox(
            "📉 Short Predictions",
            pred_files,
            index=pred_files.index(short_candidates[0]) if short_candidates else 0,
            key="sa_short",
        )

    # ── Threshold controls ──
    tcol1, tcol2, tcol3 = st.columns(3)
    with tcol1:
        long_thr = st.slider("Long Threshold", 0.40, 0.80, 0.58, 0.01, key="sa_long_thr")
    with tcol2:
        short_thr = st.slider("Short Threshold", 0.40, 0.80, 0.54, 0.01, key="sa_short_thr")
    with tcol3:
        rolling_window = st.select_slider(
            "Rolling Window (bars)",
            options=[6, 12, 24, 48, 72, 168],
            value=24,
            key="sa_window",
        )

    # ── Load data ──
    merged = load_prediction_pair(long_path, short_path)
    if merged.empty:
        st.error("Could not load or merge prediction files. Check column names.")
        return

    st.caption(f"**Loaded:** {len(merged):,} prediction bars · "
               f"{merged.index.min().strftime('%Y-%m-%d')} → {merged.index.max().strftime('%Y-%m-%d')}")

    # ── Tabs ──
    tab1, tab2, tab3 = st.tabs([
        "📊 Probability Density",
        "⚡ Conflict Matrix",
        "📈 Autocorrelation",
    ])

    # ── Tab 1: Rolling Probability Density ──
    with tab1:
        _render_probability_density(merged, long_thr, short_thr, rolling_window)

    # ── Tab 2: Conflict Matrix ──
    with tab2:
        _render_conflict_matrix(merged, long_thr, short_thr)

    # ── Tab 3: Autocorrelation ──
    with tab3:
        _render_autocorrelation(merged, long_thr, short_thr)


def _render_probability_density(
    df: pd.DataFrame, long_thr: float, short_thr: float, window: int
):
    """Rolling probability density with threshold overlays."""
    import numpy as np

    # Only use bars with actual predictions (prob > 0)
    buy_mask = df["prob_Buy"] > 0
    sell_mask = df["prob_Sell"] > 0

    # Compute rolling stats on prediction-only bars
    buy_roll = df.loc[buy_mask, "prob_Buy"].rolling(window, min_periods=1)
    sell_roll = df.loc[sell_mask, "prob_Sell"].rolling(window, min_periods=1)

    fig = go.Figure()

    # Buy probability band
    buy_mean = buy_roll.mean()
    buy_std = buy_roll.std().fillna(0)
    fig.add_trace(go.Scatter(
        x=buy_mean.index, y=(buy_mean + buy_std).values,
        mode="lines", line=dict(width=0), showlegend=False,
        hoverinfo="skip",
    ))
    fig.add_trace(go.Scatter(
        x=buy_mean.index, y=(buy_mean - buy_std).values,
        mode="lines", line=dict(width=0),
        fill="tonexty", fillcolor="rgba(100,255,218,0.12)",
        showlegend=False, hoverinfo="skip",
    ))
    fig.add_trace(go.Scatter(
        x=buy_mean.index, y=buy_mean.values,
        mode="lines", line=dict(color="#64ffda", width=2),
        name=f"prob_Buy (rolling {window})",
        hovertemplate="%{x|%Y-%m-%d %H:%M}<br>prob_Buy: %{y:.3f}<extra></extra>",
    ))

    # Sell probability band
    sell_mean = sell_roll.mean()
    sell_std = sell_roll.std().fillna(0)
    fig.add_trace(go.Scatter(
        x=sell_mean.index, y=(sell_mean + sell_std).values,
        mode="lines", line=dict(width=0), showlegend=False,
        hoverinfo="skip",
    ))
    fig.add_trace(go.Scatter(
        x=sell_mean.index, y=(sell_mean - sell_std).values,
        mode="lines", line=dict(width=0),
        fill="tonexty", fillcolor="rgba(255,107,107,0.12)",
        showlegend=False, hoverinfo="skip",
    ))
    fig.add_trace(go.Scatter(
        x=sell_mean.index, y=sell_mean.values,
        mode="lines", line=dict(color="#ff6b6b", width=2),
        name=f"prob_Sell (rolling {window})",
        hovertemplate="%{x|%Y-%m-%d %H:%M}<br>prob_Sell: %{y:.3f}<extra></extra>",
    ))

    # Threshold lines
    fig.add_hline(y=long_thr, line_dash="dot", line_color="#64ffda",
                  opacity=0.6, annotation_text=f"Long: {long_thr}")
    fig.add_hline(y=short_thr, line_dash="dot", line_color="#ff6b6b",
                  opacity=0.6, annotation_text=f"Short: {short_thr}")

    fig.update_layout(
        title="Rolling Probability Density (±1σ bands)",
        template="plotly_dark",
        paper_bgcolor="#0e1117", plot_bgcolor="#0e1117",
        xaxis_title="Date", yaxis_title="Probability",
        yaxis=dict(range=[0.3, 0.85]),
        font=dict(family="Inter", color="#ccd6f6"),
        height=500,
        legend=dict(orientation="h", y=-0.15),
    )
    st.plotly_chart(fig, use_container_width=True)

    # KPI row
    k1, k2, k3, k4 = st.columns(4)
    buy_above = (df.loc[buy_mask, "prob_Buy"] >= long_thr).sum()
    sell_above = (df.loc[sell_mask, "prob_Sell"] >= short_thr).sum()
    k1.metric("Buy Signals ≥ Threshold", f"{buy_above:,} ({buy_above/buy_mask.sum()*100:.1f}%)")
    k2.metric("Sell Signals ≥ Threshold", f"{sell_above:,} ({sell_above/sell_mask.sum()*100:.1f}%)")
    k3.metric("Buy Mean prob", f"{df.loc[buy_mask, 'prob_Buy'].mean():.4f}")
    k4.metric("Sell Mean prob", f"{df.loc[sell_mask, 'prob_Sell'].mean():.4f}")


def _render_conflict_matrix(df: pd.DataFrame, long_thr: float, short_thr: float):
    """Render conflict matrix heatmap and rolling conflict rate."""
    import numpy as np

    matrix = compute_conflict_matrix(df, long_thr, short_thr)
    if not matrix:
        st.warning("No data to compute conflict matrix.")
        return

    # ── 4-cell Heatmap ──
    z = [
        [matrix["neither"]["pct"], matrix["short_only"]["pct"]],
        [matrix["long_only"]["pct"], matrix["conflict"]["pct"]],
    ]
    text = [
        [f"Neither\n{matrix['neither']['count']:,} bars\n({matrix['neither']['pct']:.1f}%)",
         f"Short Only\n{matrix['short_only']['count']:,} bars\n({matrix['short_only']['pct']:.1f}%)"],
        [f"Long Only\n{matrix['long_only']['count']:,} bars\n({matrix['long_only']['pct']:.1f}%)",
         f"⚡ CONFLICT\n{matrix['conflict']['count']:,} bars\n({matrix['conflict']['pct']:.1f}%)"],
    ]

    fig_heat = go.Figure(go.Heatmap(
        z=z,
        x=["Short Inactive", "Short Active"],
        y=["Long Inactive", "Long Active"],
        text=text,
        texttemplate="%{text}",
        textfont=dict(size=14),
        colorscale=[[0, "#1a1a2e"], [0.5, "#0f3460"], [1, "#e94560"]],
        showscale=False,
        hoverinfo="skip",
    ))
    fig_heat.update_layout(
        title="Signal Conflict Matrix",
        template="plotly_dark",
        paper_bgcolor="#0e1117", plot_bgcolor="#0e1117",
        font=dict(family="Inter", color="#ccd6f6", size=13),
        height=350,
        xaxis=dict(side="top"),
    )
    st.plotly_chart(fig_heat, use_container_width=True)

    # KPI row
    k1, k2, k3 = st.columns(3)
    k1.metric("Total Bars", f"{matrix['total_bars']:,}")
    conflict_rate = matrix["conflict"]["pct"]
    k2.metric("Conflict Rate", f"{conflict_rate:.2f}%",
             delta=f"{matrix['conflict']['count']:,} bars",
             delta_color="inverse")
    signal_rate = matrix["long_only"]["pct"] + matrix["short_only"]["pct"] + conflict_rate
    k3.metric("Any Signal Rate", f"{signal_rate:.1f}%")

    # ── Rolling Conflict Rate ──
    st.markdown("#### Rolling Conflict Rate (7-day window)")
    long_active = df["prob_Buy"] >= long_thr
    short_active = df["prob_Sell"] >= short_thr
    conflict_series = (long_active & short_active).astype(float)
    rolling_conflict = conflict_series.rolling(168, min_periods=24).mean() * 100  # 168 bars = 7 days at 1H

    fig_rc = go.Figure()
    fig_rc.add_trace(go.Scatter(
        x=rolling_conflict.index, y=rolling_conflict.values,
        mode="lines", line=dict(color="#e94560", width=2),
        fill="tozeroy", fillcolor="rgba(233,69,96,0.15)",
        name="Conflict Rate",
        hovertemplate="%{x|%Y-%m-%d}<br>Conflict: %{y:.1f}%<extra></extra>",
    ))
    fig_rc.update_layout(
        template="plotly_dark",
        paper_bgcolor="#0e1117", plot_bgcolor="#0e1117",
        xaxis_title="Date", yaxis_title="Conflict Rate (%)",
        font=dict(family="Inter", color="#ccd6f6"),
        height=300,
    )
    st.plotly_chart(fig_rc, use_container_width=True)


def _render_autocorrelation(df: pd.DataFrame, long_thr: float, short_thr: float):
    """Render signal autocorrelation and run-length statistics."""
    long_active = (df["prob_Buy"] >= long_thr) & (df["prob_Buy"] > 0)
    short_active = (df["prob_Sell"] >= short_thr) & (df["prob_Sell"] > 0)

    acf_long = compute_autocorrelation(long_active, max_lag=24)
    acf_short = compute_autocorrelation(short_active, max_lag=24)

    # ── ACF bar chart ──
    fig_acf = go.Figure()
    fig_acf.add_trace(go.Bar(
        x=acf_long.index - 0.15, y=acf_long.values,
        width=0.3, name="Long ACF",
        marker_color="#64ffda", opacity=0.85,
    ))
    fig_acf.add_trace(go.Bar(
        x=acf_short.index + 0.15, y=acf_short.values,
        width=0.3, name="Short ACF",
        marker_color="#ff6b6b", opacity=0.85,
    ))
    fig_acf.update_layout(
        title="Signal Autocorrelation (Binary Active/Inactive)",
        template="plotly_dark",
        paper_bgcolor="#0e1117", plot_bgcolor="#0e1117",
        xaxis_title="Lag (bars)", yaxis_title="ACF",
        font=dict(family="Inter", color="#ccd6f6"),
        height=400,
        barmode="group",
        legend=dict(orientation="h", y=-0.15),
    )
    fig_acf.add_hline(y=0, line_dash="dash", line_color="#4a5568", opacity=0.4)
    st.plotly_chart(fig_acf, use_container_width=True)

    # ── Run-length statistics ──
    st.markdown("#### Consecutive Signal Run Lengths")
    run_long = compute_run_length_stats(long_active)
    run_short = compute_run_length_stats(short_active)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Long Mean Run", f"{run_long['mean_run']:.1f} bars")
    c2.metric("Long Max Run", f"{run_long['max_run']} bars")
    c3.metric("Short Mean Run", f"{run_short['mean_run']:.1f} bars")
    c4.metric("Short Max Run", f"{run_short['max_run']} bars")

    # Run distribution chart
    if run_long["run_distribution"] or run_short["run_distribution"]:
        all_lens = sorted(set(list(run_long["run_distribution"].keys()) +
                              list(run_short["run_distribution"].keys())))
        # Cap at 20 for readability
        all_lens = [l for l in all_lens if l <= 20]
        if all_lens:
            fig_runs = go.Figure()
            fig_runs.add_trace(go.Bar(
                x=[l - 0.15 for l in all_lens],
                y=[run_long["run_distribution"].get(l, 0) for l in all_lens],
                width=0.3, name="Long Runs",
                marker_color="#64ffda", opacity=0.85,
            ))
            fig_runs.add_trace(go.Bar(
                x=[l + 0.15 for l in all_lens],
                y=[run_short["run_distribution"].get(l, 0) for l in all_lens],
                width=0.3, name="Short Runs",
                marker_color="#ff6b6b", opacity=0.85,
            ))
            fig_runs.update_layout(
                title="Run Length Distribution (consecutive bars above threshold)",
                template="plotly_dark",
                paper_bgcolor="#0e1117", plot_bgcolor="#0e1117",
                xaxis_title="Run Length (bars)", yaxis_title="Count",
                font=dict(family="Inter", color="#ccd6f6"),
                height=350,
                barmode="group",
                legend=dict(orientation="h", y=-0.15),
            )
            st.plotly_chart(fig_runs, use_container_width=True)

    # Interpretation
    st.markdown("---")
    if run_long["mean_run"] > 0 or run_short["mean_run"] > 0:
        st.markdown(
            f"**Interpretation:** Long signals cluster in runs of **{run_long['mean_run']:.1f}** bars "
            f"(max {run_long['max_run']}), Short in runs of **{run_short['mean_run']:.1f}** bars "
            f"(max {run_short['max_run']}). "
            f"{'High ACF at lag 1–4 confirms clustered signals — `consecutive_signal_threshold` is effective.' if acf_long.iloc[:4].mean() > 0.3 or acf_short.iloc[:4].mean() > 0.3 else 'Low ACF suggests scattered signals — `consecutive_signal_threshold` may filter out too many entries.'}"
        )


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

    # Section 3 — Model Registry
    st.markdown("---")
    render_model_registry()

    # Section 4 — Signal Analysis
    st.markdown("---")
    render_signal_analysis()


if __name__ == "__main__":
    main()
