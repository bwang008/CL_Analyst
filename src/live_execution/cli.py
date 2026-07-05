"""
CLI entry point for the CL Analyst live trading engine.

Extracted from live_trader.py (Phase 1 modularization).
Contains argument parsing, strategy resolution, and the main() function.

Usage:
    conda run -n trader python -m src.live_execution.live_trader
    conda run -n trader python -m src.live_execution.cli --config configs/strategies/my.json
"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

import pandas as pd

from src.data_paths import get_data_root as _dp_data_root
from src.live_execution.log_config import _setup_file_logging

log = logging.getLogger("LiveTrader")


# ---------------------------------------------------------------------------
# Legacy cache migration
# ---------------------------------------------------------------------------

def _merge_legacy_cid_caches(shared_cache_path: str) -> None:
    """Merge any per-client_id warm-start caches into the shared cache.

    Prior versions created separate caches per client_id
    (warm_start_cache_cid18.parquet, etc.). This function detects those
    files, merges new bars into the shared cache, and renames the old
    files so they aren't re-processed.
    """
    import glob as _glob

    shared = Path(shared_cache_path)
    cache_dir = shared.parent
    pattern = str(cache_dir / "warm_start_cache_cid*.parquet")
    legacy_files = sorted(_glob.glob(pattern))

    if not legacy_files:
        return

    log.info(
        "Found %d legacy per-client cache(s) — merging into shared cache",
        len(legacy_files),
    )

    # Load the shared cache if it exists
    if shared.exists():
        shared_df = pd.read_parquet(shared, engine="pyarrow")
        if "DateTime" in shared_df.columns:
            shared_df["DateTime"] = pd.to_datetime(shared_df["DateTime"])
    else:
        shared_df = pd.DataFrame(
            columns=["DateTime", "Open", "High", "Low", "Close", "Volume"]
        )

    # Merge bars from each legacy file
    merged_count = 0
    for legacy_path in legacy_files:
        try:
            ldf = pd.read_parquet(legacy_path, engine="pyarrow")
            if "DateTime" in ldf.columns:
                ldf["DateTime"] = pd.to_datetime(ldf["DateTime"])
            before = len(shared_df)
            shared_df = pd.concat([shared_df, ldf], ignore_index=True)
            shared_df = shared_df.drop_duplicates(subset="DateTime", keep="last")
            new_bars = len(shared_df) - before
            merged_count += new_bars
            log.info(
                "  Merged %s: %d bars (%d new)",
                Path(legacy_path).name, len(ldf), new_bars,
            )
            # Rename legacy file so it isn't re-processed
            backup = Path(legacy_path).with_suffix(".parquet.migrated")
            Path(legacy_path).rename(backup)
            log.info("  Renamed -> %s", backup.name)
        except Exception as e:
            log.warning("  Failed to merge %s: %s", legacy_path, e)

    if merged_count > 0:
        shared_df = shared_df.sort_values("DateTime").reset_index(drop=True)
        shared.parent.mkdir(parents=True, exist_ok=True)
        shared_df.to_parquet(shared, engine="pyarrow", index=False)
        log.info(
            "Shared cache updated: %d total bars (%d new from migration)",
            len(shared_df), merged_count,
        )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    # Deferred imports to avoid circular dependencies and keep module light
    from src.live_execution.live_trader import (
        LiveTrader,
        _STRATEGY_REGISTRY,
        _DEFAULT_STRATEGY,
        _DEFAULT_DB_PATH,
        _DEFAULT_QUANTITY,
    )
    from src.live_execution.strategies.configurable_strategy import (
        ConfigurableStrategy,
    )

    available = ", ".join(sorted(_STRATEGY_REGISTRY.keys()))
    default_host = os.environ.get("IBKR_HOST", "127.0.0.1")
    default_port = int(os.environ.get("IBKR_PORT", "4002"))
    parser = argparse.ArgumentParser(
        description="CL Analyst — Live Execution Engine"
    )
    parser.add_argument(
        "--host", default=default_host,
        help="IBKR TWS/Gateway host (default: IBKR_HOST or 127.0.0.1)",
    )
    parser.add_argument(
        "--data-source", default="ibkr",
        help="Data source provider (default: ibkr)",
    )
    parser.add_argument(
        "--exec-source", default="ibkr",
        help="Execution routing provider (default: ibkr)",
    )
    parser.add_argument(
        "--data-port", type=int, default=4001,
        help="Data feed primary port (default: 4001 for IB Live Gateway; falls back to 7496 TWS)",
    )
    parser.add_argument(
        "--exec-port", type=int, default=4002,
        help="Execution primary port (default: 4002 for IB Paper Gateway; falls back to 7497 TWS)",
    )
    parser.add_argument(
        "--client-id", type=int, default=1,
        help="IBKR client ID (default: 1; overridden by config live_config.client_id)",
    )
    parser.add_argument(
        "--strategy", default=_DEFAULT_STRATEGY,
        help=f"Strategy to use (available: {available}; default: {_DEFAULT_STRATEGY})",
    )
    parser.add_argument(
        "--config", default=None,
        help="Path to a strategy JSON config file (overrides --strategy)",
    )
    parser.add_argument(
        "--db-path", default=_DEFAULT_DB_PATH,
        help="Path to the telemetry SQLite database",
    )
    parser.add_argument(
        "--quantity", type=int, default=_DEFAULT_QUANTITY,
        help="Number of CL contracts per trade (default: 1)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Run without placing real orders (log signals only)",
    )
    parser.add_argument(
        "--seed-path", default=None,
        help=(
            "Path to the immutable seed CSV (default: derived from the "
            "config's execution symbol; CL keeps legacy names)"
        ),
    )
    parser.add_argument(
        "--cache-path", default=None,
        help=(
            "Path to the warm-start Parquet cache (default: derived from "
            "the config's execution symbol; CL keeps legacy names)"
        ),
    )
    parser.add_argument(
        "--entry-mode", default=None,
        choices=["adaptive", "marketable_limit", "market"],
        help=(
            "Entry order type: 'adaptive' (IBKR algo, default), "
            "'marketable_limit' (limit 2 ticks through NBBO), "
            "'market' (plain MKT). Overrides live_config.entry_mode in JSON."
        ),
    )
    parser.add_argument(
        "--adaptive-priority", default=None,
        choices=["Normal", "Urgent", "Patient"],
        help="Adaptive algo urgency (default: Normal). Only used with --entry-mode adaptive.",
    )

    args = parser.parse_args()

    # Resolve strategy: --config takes priority over --strategy
    config_client_id: int | None = None
    config_entry_mode: str | None = None
    config_adaptive_priority: str | None = None
    config_exit_mode: str | None = None
    if args.config is not None:
        strategy = ConfigurableStrategy(
            config_path=args.config,
            base_quantity=args.quantity,
        )
        # Read live_config overrides from the strategy JSON
        live_cfg = strategy.config.get("live_config", {})
        config_client_id = live_cfg.get("client_id")
        config_entry_mode = live_cfg.get("entry_mode")
        config_adaptive_priority = live_cfg.get("adaptive_priority")
        config_exit_mode = live_cfg.get("exit_mode")
    else:
        if args.strategy is None:
            parser.error("You must provide a --config path. Legacy strategies have been removed.")
        strategy_key = args.strategy.upper()
        if strategy_key not in _STRATEGY_REGISTRY:
            parser.error(
                f"Unknown strategy '{args.strategy}'. "
                f"Available: {available}"
            )
        strategy_cls = _STRATEGY_REGISTRY[strategy_key]
        strategy = strategy_cls(base_quantity=args.quantity)

    # ── Fail-fast instrument resolution (T1) ──────────────────────
    # Resolve + validate the instrument immediately after config load,
    # BEFORE any data/exec factory constructs an IBKR client. Any
    # ValueError propagates and kills the process pre-connect.
    from src.live_execution.instrument_context import resolve_instrument_context

    ctx = resolve_instrument_context(strategy.config)
    log.info(
        "Instrument resolved: execution=%s (%s, tick=%s) brain=%s",
        ctx.execution_symbol, ctx.execution_instrument.exchange,
        ctx.execution_instrument.tick_size, ctx.brain_symbol,
    )

    # CLI --client-id takes priority; if not explicitly set (== 1 default),
    # fall back to config's live_config.client_id
    resolved_client_id = args.client_id
    if resolved_client_id == 1 and config_client_id is not None:
        resolved_client_id = config_client_id

    # ── Per-symbol data-path defaults (T2) ────────────────────────
    # Derived from the config's BRAIN symbol via the single naming
    # authority (CL keeps its legacy names byte-for-byte); explicit
    # --seed-path/--cache-path always win.
    from src.live_execution.data_manager import derive_data_paths

    paths = derive_data_paths(ctx.brain_symbol)
    resolved_seed_path = args.seed_path or str(paths.seed_5m)

    # ── Per-strategy isolation ────────────────────────────────────
    # Telemetry DB is per-client (contains strategy-specific signals,
    # predictions, trades). OHLCV warm-start cache is SHARED per brain
    # symbol — all strategies on the same brain symbol receive the same
    # continuous bars.
    resolved_db_path = args.db_path
    resolved_cache_path = args.cache_path or str(paths.cache_5m)  # shared (no cid suffix)

    if resolved_client_id != 1:
        cid_suffix = f"_cid{resolved_client_id}"

        # Only override DB path if user hasn't explicitly set a custom path
        if resolved_db_path == _DEFAULT_DB_PATH:
            resolved_db_path = str(
                _dp_data_root() / f"live_telemetry{cid_suffix}.db"
            )

        # Merge any existing per-client caches into the shared cache
        # so no historical bars are lost from prior per-cid runs.
        # C8: legacy warm_start_cache_cid*.parquet files are CL bars by
        # definition — only brain=CL instances (CL and MCL configs) may
        # merge them. Merging them into a non-CL cache would be silent
        # misdata.
        if ctx.brain_symbol == "CL":
            _merge_legacy_cid_caches(resolved_cache_path)

        log.info(
            "Multi-instance isolation: client_id=%d  "
            "db=%s  cache=%s (shared)",
            resolved_client_id,
            Path(resolved_db_path).name,
            Path(resolved_cache_path).name,
        )

    # ── IBKR subscription advisory ───────────────────────────────
    if resolved_client_id != 1:
        log.info(
            "NOTE: This instance (client_id=%d) creates its own IBKR "
            "data subscriptions. With many concurrent strategies, "
            "consider a shared data broadcaster.",
            resolved_client_id,
        )

    # ── Resolve entry_mode: CLI > config > default ────────────────
    resolved_entry_mode = args.entry_mode
    if resolved_entry_mode is None:
        resolved_entry_mode = config_entry_mode or "adaptive"

    resolved_adaptive_priority = args.adaptive_priority
    if resolved_adaptive_priority is None:
        resolved_adaptive_priority = config_adaptive_priority or "Normal"

    # ── Resolve exit_mode: config > default ────────────────────────
    resolved_exit_mode = config_exit_mode or "market"

    from src.live_execution.factories import DataFeedFactory, ExecutionFactory

    # T2: the data feed binds its historical-fetch symbol at construction
    # (instrument_context passes through the factory's **kwargs).
    data_client = DataFeedFactory.create(args.data_source, host=args.host, port=args.data_port, client_id=resolved_client_id, instrument_context=ctx)
    exec_client = ExecutionFactory.create(args.exec_source, host=args.host, port=args.exec_port, client_id=resolved_client_id + 1)

    trader = LiveTrader(
        data_client=data_client,
        exec_client=exec_client,
        strategy=strategy,
        db_path=resolved_db_path,
        seed_path=resolved_seed_path,
        cache_path=resolved_cache_path,
        quantity=args.quantity,
        dry_run=args.dry_run,
        entry_mode=resolved_entry_mode,
        adaptive_priority=resolved_adaptive_priority,
        exit_mode=resolved_exit_mode,
    )
    # Enable persistent file logging now that client_id is resolved
    _setup_file_logging(resolved_client_id)

    trader.start()


if __name__ == "__main__":
    main()
