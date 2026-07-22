"""
BacktestEngine — Config-Driven Trade Management Backtester.

Master backtesting engine for CL futures strategies.  All strategy
parameters are loaded from a JSON config file (configs/strategies/*.json).

Capabilities:
- Single-position FSM mode (FLAT / IN_POSITION)
- Concurrent multi-position mode (configurable max open positions)
- Time-barrier exit (configurable max hold bars)
- Trailing stop to breakeven (+N×ATR in favor → SL moves to entry)
- Post-exit re-entry cooldown: flavor-blind per-side ``cooldown_bars``,
  enforced by the execution strategy's re-gate (e.g. TieredEnsembleStrategy)
  reading the engine's last_exit_bars_ago counters — armed by _close_trade
  for EVERY exit reason (the engine-level flavored tp/sl_cooldown_bars were
  removed in 3d95040)
- Gap-aware slippage (fills at Open when bar gaps past stop)
- Exit-trigger overlays, DEFAULT-OFF / backtest-only (ticket
  exit-triggers-eod-oppsignal_07072026_1924; see README "Exit-Trigger
  Overlays"):
    * ``weekend_flatten`` — flatten a winner on the last bar before a
      weekend/holiday gap (data-driven: gap-to-next-bar >= 40h)
    * ``eod_flatten`` — flatten a winner on the last bar before the daily
      session halt (gap band [2h, weekend threshold), disjoint from weekend)
    * both gate on unrealized >= profit_atr_mult × ATR-at-entry, fire AFTER
      TP/SL and TIME_BARRIER, fill at bar open; ExitReasons
      WEEKEND_FLATTEN / EOD_FLATTEN
- Opposite-signal profit-close: conflict_resolution mode
  "close_existing_position_if_profit" (TieredEnsembleStrategy) — EXIT when
  the opposite signal fires, own side stops confirming, and the position is
  green on the EXEC price basis (EngineState.floating_pnl_points, engine-fed)
- A/B harness for the above: agent/ab_exit_triggers.py

Usage:
    conda activate trader
    python agent/backtest_engine.py --config configs/strategies/manatee.json --predictions reports/exp017_long_predictions.csv --data data/processed/CL_set_06.parquet
    python agent/backtest_engine.py --config configs/strategies/manatee.json --predictions ... --live-data data/live_session_feed.parquet

Telemetry DBs (for comparing backtest vs live):
    live_telemetry.db        - Manatee (client_id default), Feb 25 - Mar 2 2026
    live_telemetry_cid10.db  - Manatee (client_id 10), Mar 3 - Mar 5 2026
    live_telemetry_cid13.db  - ManateeKoala_Conservative ensemble (client_id 13), Mar 2 - Mar 6 2026
    Tables: market_bars, trade_ledger, raw_front_month_bars, shadow_log (features_json + prob_buy/prob_sell), tradebook_events

Known Parity Gap (2026-03-08 analysis):
    52/80 features diverge >2σ between live system and processed parquet.
    Worst: LIQ_AMIHUD_10080 (163x), VOL_YZ_4032 (136x), MACRO_WIDTH_3M (63x).
    Root causes:
      1. Lookback mismatch: live warm start = 60 days, parquet = 15+ years of history
         → long-window indicators (10080 bars = 35 days) are most affected
      2. OHLCV source: live uses IBKR continuous contract, parquet uses cl-5m_bk.csv
      3. Normalization: parquet normalizes globally; live computes features on-the-fly
    Impact: Buy model prob_buy maxes at 0.45 live (never fires at 0.6 threshold),
            but backtest shows 68% WR on the same model. Sell model IS firing live.

Author: CL Analyst
"""


from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional

import numpy as np
import pandas as pd
from pandas.tseries.offsets import DateOffset

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from src.LGBMLearner import LGBMLearner
from src.util import get_X_y
from src.live_execution.strategies.execution_models import (
    BaseExecutionStrategy,
    EngineState,
    Order,
    HOLD,
    allocate_tranche_lots,
    create_execution_strategy,
)
from src.live_execution.execution_guard import ExecutionGuard


# ---------------------------------------------------------------------------
# Enums & Data Structures
# ---------------------------------------------------------------------------


class TradeState(Enum):
    """Finite State Machine states for trade management.

    Transitions:
        FLAT ──[buy signal ≥ threshold]──→ IN_POSITION
        IN_POSITION ──[TP hit]──→ FLAT
        IN_POSITION ──[SL hit]──→ FLAT
        IN_POSITION ──[trailing stop hit]──→ FLAT
        IN_POSITION ──[288 bars elapsed]──→ FLAT
    """

    FLAT = auto()
    IN_POSITION = auto()


class ExitReason(Enum):
    """Why a trade was closed."""

    TP = "TP"
    SL = "SL"
    TRAILING_BE = "TRAILING_BE"  # Trailing stop hit at breakeven
    TIME_BARRIER = "TIME_BARRIER"  # 288 bars elapsed
    SIGNAL_EXIT = "SIGNAL_EXIT"  # Strategy requested exit (conflict resolution)
    WEEKEND_FLATTEN = "WEEKEND_FLATTEN"  # Optional overlay: profitable winner flattened before a weekend gap
    EOD_FLATTEN = "EOD_FLATTEN"  # Optional overlay: profitable winner flattened before the daily session halt


def _normalize_trailing_ladder(
    rungs, label: str
) -> Optional[tuple[tuple[float, float], ...]]:
    """Normalize a trailing ladder into ((activation, lock), ...) tuples.

    Accepts (activation, lock) pairs or {"activation_atr", "lock_atr"} dicts.
    Structural validation only (monotonicity, lock < activation); the
    TP-relative and legacy-scalar consistency checks live in
    ``strategy_config.parse_trailing_ladder`` where the side context exists.
    """
    if rungs is None:
        return None
    norm: list[tuple[float, float]] = []
    for r in rungs:
        if isinstance(r, dict):
            norm.append((float(r["activation_atr"]), float(r["lock_atr"])))
        else:
            a, o = r
            norm.append((float(a), float(o)))
    if not norm:
        raise ValueError(f"{label} must contain at least one rung (pass None to disable)")
    prev_a = prev_o = float("-inf")
    for i, (a, o) in enumerate(norm):
        if o >= a:
            raise ValueError(f"{label}[{i}] lock ({o}) must be strictly below activation ({a})")
        if a <= prev_a:
            raise ValueError(f"{label} activations must be strictly increasing (rung {i})")
        if o <= prev_o:
            raise ValueError(f"{label} locks must be strictly increasing (rung {i})")
        prev_a, prev_o = a, o
    return tuple(norm)


@dataclass
class TradeRecord:
    """Result of a single completed trade."""

    entry_dt: pd.Timestamp
    exit_dt: pd.Timestamp
    entry_price: float
    exit_price: float
    entry_fill: float
    exit_fill: float
    side: int  # +1 long, -1 short
    atr_at_entry: float
    exit_reason: ExitReason
    duration_bars: int
    gross_pnl_dollars: float
    commission_dollars: float
    net_pnl_dollars: float
    lots: int = 1
    initial_tp_price: float = 0.0
    initial_sl_price: float = 0.0
    # Per-tranche closes ({lots, exit_dt, exit_price, exit_fill, reason})
    # for multi-tranche positions only; None otherwise. EXCLUDED from
    # to_dataframe()/to_csv() — the unified ledger schema is frozen.
    tranche_exits: Optional[list] = None


@dataclass
class BacktestResult:
    """Aggregate results from a backtest run."""

    trades: list[TradeRecord] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)
    label: str = ""
    start_dt: pd.Timestamp | None = None
    end_dt: pd.Timestamp | None = None
    blocked_trades_count: int = 0

    @property
    def total_pnl(self) -> float:
        return sum(t.net_pnl_dollars for t in self.trades)

    @property
    def trade_count(self) -> int:
        return len(self.trades)

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        wins = sum(1 for t in self.trades if t.net_pnl_dollars > 0)
        return wins / len(self.trades)

    @property
    def profit_factor(self) -> float:
        gross_profit = sum(
            t.net_pnl_dollars for t in self.trades if t.net_pnl_dollars > 0
        )
        gross_loss = abs(
            sum(t.net_pnl_dollars for t in self.trades if t.net_pnl_dollars < 0)
        )
        if gross_loss == 0:
            return float("inf") if gross_profit > 0 else 0.0
        return gross_profit / gross_loss

    @property
    def max_drawdown(self) -> float:
        """Peak-to-trough drawdown from the bar-by-bar equity curve.

        Uses the full equity curve (realized + floating PnL) when
        available, falling back to closed-trade PnL when it is not.
        """
        if self.equity_curve:
            curve = np.array(self.equity_curve)
            running_max = np.maximum.accumulate(curve)
            drawdowns = curve - running_max
            return float(np.min(drawdowns))
        if not self.trades:
            return 0.0
        cum_pnl = np.cumsum([t.net_pnl_dollars for t in self.trades])
        running_max = np.maximum.accumulate(cum_pnl)
        drawdowns = cum_pnl - running_max
        return float(np.min(drawdowns))

    @property
    def exit_distribution(self) -> dict[str, dict[str, float]]:
        """Return count and percentage for each exit reason."""
        total = len(self.trades)
        if total == 0:
            return {}
        counts: dict[str, int] = {}
        for t in self.trades:
            key = t.exit_reason.value
            counts[key] = counts.get(key, 0) + 1
        return {
            reason: {"count": count, "pct": count / total * 100}
            for reason, count in counts.items()
        }

    def to_dataframe(self) -> pd.DataFrame:
        """Export trades as a DataFrame with the unified trade ledger schema.

        Column schema matches the reconciliation pipeline so that
        backtest and live ledgers can be programmatically diffed.
        """
        if not self.trades:
            return pd.DataFrame()
        rows = []
        for t in self.trades:
            rows.append({
                "entry_time": t.entry_dt,
                "signal_side": "LONG" if t.side == 1 else "SHORT",
                "entry_price": t.entry_price,
                "entry_fill": t.entry_fill,
                "initial_tp_price": t.initial_tp_price,
                "initial_sl_price": t.initial_sl_price,
                "exit_time": t.exit_dt,
                "exit_price": t.exit_price,
                "exit_fill": t.exit_fill,
                "exit_reason": t.exit_reason.value,
                "atr_at_entry": t.atr_at_entry,
                "duration_bars": t.duration_bars,
                "lots": t.lots,
                "gross_pnl_dollars": t.gross_pnl_dollars,
                "commission_dollars": t.commission_dollars,
                "net_pnl_dollars": t.net_pnl_dollars,
            })
        return pd.DataFrame(rows)

    def to_csv(self, path: str) -> None:
        """Export trades to CSV using the unified trade ledger schema."""
        df = self.to_dataframe()
        df.to_csv(path, index=False)
        print(f"Exported {len(df)} trades to {path}")


# ---------------------------------------------------------------------------
# Open Position Tracking (for concurrent mode)
# ---------------------------------------------------------------------------


@dataclass
class _OpenPosition:
    """State for a single open position (used in concurrent mode)."""

    entry_dt: pd.Timestamp
    entry_price: float
    entry_fill: float
    atr_at_entry: float
    side: int
    tp_price: float
    sl_price: float
    original_sl_price: float
    trailing_rung: int = 0  # rungs consumed (0 = trailing not activated)
    bars_held: int = 0
    highest_high: float = 0.0
    lowest_low: float = float("inf")
    lots: int = 1
    # Per-trade overrides (None = use engine global)
    pos_max_horizon: Optional[int] = None
    # Resolved trailing ladder for this position ((activation, lock), ...)
    pos_trailing_ladder: tuple = ()


# ---------------------------------------------------------------------------
# Backtest Engine
# ---------------------------------------------------------------------------


# Keep old name as alias for backward-compatibility with existing imports
class BacktestEngine:
    """Config-driven bar-by-bar backtester with FSM trade management.

    Supports two modes controlled by ``allow_concurrent``:

    **Single-position mode** (default, ``allow_concurrent=False``):
        FLAT → IN_POSITION → FLAT
        Only one trade at a time.  Matches live trader behaviour.
        Post-exit cooldown is NOT an engine concern: the engine only
        maintains last_exit_bars_ago counters (reset in _close_trade for
        every exit reason); the execution strategy's re-gate enforces the
        per-side ``cooldown_bars`` window.

    **Concurrent mode** (``allow_concurrent=True``):
        Accepts new signals while positions are open (up to
        ``max_concurrent``).  Each position is independently managed
        with TP/SL/trailing/time-barrier.  No cooldown between trades.

    Args:
        tp_atr_mult: ATR multiplier for take-profit barrier.
        sl_atr_mult: ATR multiplier for stop-loss barrier.
        max_horizon: Max bars to hold a position (time barrier).
        trailing_atr_mult: ATR move in favor to trigger trailing stop.
        trailing_sl_atr_offset: After trailing triggers, SL moves to
                           entry + offset×ATR (0 = breakeven, 0.25 = small profit).
        atr_period: Period for ATR calculation.
        commission_per_side: Flat commission per side in dollars.
        slippage_per_side: Slippage penalty per side in price units.
        contract_multiplier: Dollar value per 1.0 price move (CL = 1000).
        prob_threshold: Minimum probability to accept a buy signal.
        allow_concurrent: If True, allow multiple simultaneous positions.
        max_concurrent: Max open positions in concurrent mode.
    """

    def __init__(
        self,
        *,
        tp_atr_mult: float = 2.0,
        sl_atr_mult: float = 1.0,
        max_horizon: int = 288,
        trailing_atr_mult: float = 100.0,
        trailing_sl_atr_offset: float = 0.25,
        atr_period: int = 14,
        atr_period_long: Optional[int] = None,
        atr_period_short: Optional[int] = None,
        trailing_sl_atr_offset_long: Optional[float] = None,
        trailing_sl_atr_offset_short: Optional[float] = None,
        trailing_ladder_long=None,
        trailing_ladder_short=None,
        commission_per_side: float = 2.50,
        slippage_per_side: float = 0.01,
        contract_multiplier: float = 1000.0,
        prob_threshold: float = 0.45,
        allow_concurrent: bool = False,
        max_concurrent: int = 1,
        execution_strategy: Optional[BaseExecutionStrategy] = None,
        consecutive_signal_threshold: int = 0,
        execution_guard: Optional[ExecutionGuard] = None,
        weekend_flatten: Optional["WeekendFlattenConfig"] = None,
        eod_flatten: Optional["WeekendFlattenConfig"] = None,
        tranche_rungs_long: Optional[tuple[tuple[float, float], ...]] = None,
        tranche_rungs_short: Optional[tuple[tuple[float, float], ...]] = None,
    ) -> None:
        # Multi-rung tiered-exit (tranche) ladders are single-position-mode
        # only: the concurrent path has no tranche machinery, so accepting
        # one there would silently backtest single-rung economics.
        if (tranche_rungs_long or tranche_rungs_short) and (
            allow_concurrent or max_concurrent > 1
        ):
            raise ValueError(
                "multi-rung tiered_exits: tranche exits v1 are "
                "single-position mode only (allow_concurrent / "
                "max_concurrent > 1 is unsupported)"
            )
        self.tp_atr_mult = tp_atr_mult
        self.sl_atr_mult = sl_atr_mult
        self.max_horizon = max_horizon
        self.trailing_atr_mult = trailing_atr_mult
        self.trailing_sl_atr_offset = trailing_sl_atr_offset
        self.atr_period = atr_period
        # Per-side ATR periods (fall back to global atr_period)
        self.atr_period_long: int = atr_period_long if atr_period_long is not None else atr_period
        self.atr_period_short: int = atr_period_short if atr_period_short is not None else atr_period
        # Per-side trailing SL offset (fall back to global trailing_sl_atr_offset)
        self.trailing_sl_atr_offset_long: float = trailing_sl_atr_offset_long if trailing_sl_atr_offset_long is not None else trailing_sl_atr_offset
        self.trailing_sl_atr_offset_short: float = trailing_sl_atr_offset_short if trailing_sl_atr_offset_short is not None else trailing_sl_atr_offset
        # Per-side trailing ladders (None = single legacy rung, unchanged behavior)
        self.trailing_ladder_long = _normalize_trailing_ladder(
            trailing_ladder_long, "trailing_ladder_long"
        )
        self.trailing_ladder_short = _normalize_trailing_ladder(
            trailing_ladder_short, "trailing_ladder_short"
        )
        self.commission_per_side = commission_per_side
        self.slippage_per_side = slippage_per_side
        self.contract_multiplier = contract_multiplier
        self.prob_threshold = prob_threshold
        self.allow_concurrent = allow_concurrent
        self.max_concurrent = max(1, max_concurrent)

        # Pluggable execution strategy (None = legacy signal_sides fallback)
        self._execution_strategy = execution_strategy

        # Global execution guard (blocks new entries during toxic periods)
        self._execution_guard = execution_guard

        # Optional flatten-trigger overlays (None = off; unchanged behavior).
        # ``_flatten_bars`` (weekend) and ``_eod_flatten_bars`` (daily halt)
        # are (re)computed per run() from the OHLCV bar spacing; the two gap
        # bands are disjoint by construction.
        self._weekend_flatten = weekend_flatten
        self._eod_flatten = eod_flatten
        self._flatten_bars: set[pd.Timestamp] = set()
        self._eod_flatten_bars: set[pd.Timestamp] = set()

        # Per-side (qty_pct, tp_atr_mult) tranche ladders (None = feature off,
        # single-exit path bit-for-bit).  Validated in _parse_tranche_exits.
        self.tranche_rungs_long = (
            tuple(tranche_rungs_long) if tranche_rungs_long else None
        )
        self.tranche_rungs_short = (
            tuple(tranche_rungs_short) if tranche_rungs_short else None
        )

        # Mutable engine state (allocated once, reused across bars)
        self._engine_state = EngineState()

        # FSM state — single-position mode (reset per run)
        self._state: TradeState = TradeState.FLAT
        self._entry_dt: Optional[pd.Timestamp] = None
        self._entry_price: float = 0.0
        self._entry_fill: float = 0.0
        self._atr_at_entry: float = 0.0
        self._side: int = 0
        self._tp_price: float = 0.0
        self._sl_price: float = 0.0
        self._original_sl_price: float = 0.0
        self._trailing_rung: int = 0  # rungs consumed (0 = trailing not activated)
        self._bars_held: int = 0
        self._highest_high: float = 0.0
        self._lowest_low: float = float("inf")
        self._lots: int = 1
        # Multi-tranche state (active only when a side ladder allocates
        # >= 2 nonzero tranches for the position's lots — S1)
        self._multi_tranche: bool = False
        self._tranche_open: list[tuple[int, float]] = []  # (lots, tp_price)
        self._tranche_closed: list[dict] = []
        self._remaining_lots: int = 0
        self._trades: list[TradeRecord] = []
        # Per-trade overrides (set at entry from Order, reset on close)
        self._trade_max_horizon: int = max_horizon
        self._trade_trailing_atr_mult: float = trailing_atr_mult
        self._trade_trailing_sl_atr_offset: float = trailing_sl_atr_offset
        self._trade_trailing_ladder: tuple[tuple[float, float], ...] = (
            (trailing_atr_mult, trailing_sl_atr_offset),
        )

        # Concurrent mode state
        self._open_positions: list[_OpenPosition] = []
        self._blocked_trades_count: int = 0

    @classmethod
    def from_config(cls, cfg: dict, **overrides) -> "BacktestEngine":
        """Create a BacktestEngine from a strategy config dict.

        Reads all supported fields from the JSON config, with CLI overrides
        taking precedence.  Also instantiates the appropriate execution
        strategy via the registry/factory pattern.
        """
        from src.live_execution.strategy_config import StrategyConfig

        # Instantiate the execution strategy from config
        strategy = create_execution_strategy(cfg)

        # Safety check: warn if top-level params are shadowed by tier overrides
        cls._check_parameter_shadowing(cfg, strategy)

        # Multi-rung tiered_exits ladders — validated here (S4); None for
        # absent/single-rung sides (identity: zero new checks, zero state).
        tranche_long, tranche_short = cls._parse_tranche_exits(cfg)

        # Instantiate execution guard if risk filter keys are present
        guard = None
        if cfg.get("blocked_entry_hours_est") or cfg.get("block_long_weekends") or cfg.get("blocked_entry_hours_by_day"):
            guard = ExecutionGuard(cfg)

        sc = StrategyConfig.from_dict(cfg)
        kwargs = {
            "tp_atr_mult": sc.tp_atr_mult,
            "sl_atr_mult": sc.sl_atr_mult,
            "prob_threshold": sc.entry_threshold,
            "allow_concurrent": sc.allow_concurrent,
            "max_concurrent": sc.max_concurrent,
            "max_horizon": sc.max_hold_bars,
            "atr_period": sc.atr_period,
            "atr_period_long": sc.long.atr_period,
            "atr_period_short": sc.short.atr_period,
            "trailing_atr_mult": sc.trailing_atr_mult,
            "trailing_sl_atr_offset": sc.trailing_sl_atr_offset,
            "trailing_sl_atr_offset_long": sc.long.trailing_sl_atr_offset,
            "trailing_sl_atr_offset_short": sc.short.trailing_sl_atr_offset,
            "trailing_ladder_long": sc.long.trailing_ladder,
            "trailing_ladder_short": sc.short.trailing_ladder,
            "execution_strategy": strategy,
            "execution_guard": guard,
            "weekend_flatten": sc.weekend_flatten,
            "eod_flatten": sc.eod_flatten,
            "tranche_rungs_long": tranche_long,
            "tranche_rungs_short": tranche_short,
        }
        kwargs.update(overrides)
        return cls(**kwargs)

    @staticmethod
    def _parse_tranche_exits(
        cfg: dict,
    ) -> tuple[
        Optional[tuple[tuple[float, float], ...]],
        Optional[tuple[tuple[float, float], ...]],
    ]:
        """Parse and validate per-side multi-rung ``tiered_exits`` (S1/S4).

        Returns ``(long_rungs, short_rungs)`` where each side is a tuple of
        ``(qty_pct, tp_atr_mult)`` rungs when it declares >= 2, else None.
        Absent/single-rung sides get ZERO new validation (identity fence).
        Validation crashes loudly — never clamps: qty_pct must sum to 1.0
        (within 1e-6) and tp_atr_mult must be strictly increasing.
        """
        parsed: list[Optional[tuple[tuple[float, float], ...]]] = []
        for side_key in ("long", "short"):
            side_cfg = cfg.get(side_key) or {}
            exits = side_cfg.get("tiered_exits") or []
            if len(exits) < 2:
                parsed.append(None)
                continue
            rungs: list[tuple[float, float]] = []
            for i, rung in enumerate(exits):
                if (
                    not isinstance(rung, dict)
                    or "qty_pct" not in rung
                    or "tp_atr_mult" not in rung
                ):
                    raise ValueError(
                        f"{side_key}.tiered_exits[{i}] must declare both "
                        f"'qty_pct' and 'tp_atr_mult' (got {rung!r})"
                    )
                rungs.append(
                    (float(rung["qty_pct"]), float(rung["tp_atr_mult"]))
                )
            pct_sum = sum(p for p, _ in rungs)
            if abs(pct_sum - 1.0) > 1e-6:
                raise ValueError(
                    f"{side_key}.tiered_exits qty_pct values must sum to "
                    f"1.0 (got {pct_sum})"
                )
            prev_tp = float("-inf")
            for i, (_, tp) in enumerate(rungs):
                if tp <= prev_tp:
                    raise ValueError(
                        f"{side_key}.tiered_exits tp_atr_mult must be "
                        f"strictly increasing (rung {i}: {tp} <= {prev_tp})"
                    )
                prev_tp = tp
            parsed.append(tuple(rungs))
        return parsed[0], parsed[1]

    @staticmethod
    def _check_parameter_shadowing(cfg: dict, strategy) -> None:
        """Raise an error if side-level parameters differ from tier overrides.

        In a properly generated config (via apply_trial_params), the side-level
        values (e.g., cfg['long']['tp_atr_mult']) must match the tier-level values
        (e.g., cfg['long']['tiers'][0]['tp_atr_mult']).
        Top-level values are global fallbacks and are allowed to differ.
        """
        from src.live_execution.strategies.execution_models import TieredEnsembleStrategy

        if not isinstance(strategy, TieredEnsembleStrategy):
            return

        keys_to_check = [
            "tp_atr_mult", "sl_atr_mult", "trailing_atr_mult",
            "max_hold_bars", "cooldown_bars", "consecutive_signal_threshold"
        ]

        errors = []

        for key in keys_to_check:
            for side_key in ("long", "short"):
                side_cfg = cfg.get(side_key, {})
                side_val = side_cfg.get(key)
                
                # Cooldown and consecutive are not in tiers, so tier_val is side_val
                if key in ("cooldown_bars", "consecutive_signal_threshold"):
                    continue
                else:
                    tier_val = None
                    exits = side_cfg.get("tiered_exits", [])
                    if exits and key in exits[0]:
                        tier_val = exits[0].get(key)
                    else:
                        tiers = side_cfg.get("tiers", [])
                        if tiers and key in tiers[0]:
                            tier_val = tiers[0].get(key)

                if tier_val is not None and side_val is not None and tier_val != side_val:
                    errors.append(f"{side_key} tier {key}={tier_val} conflicts with {side_key}.{key}={side_val}")

        if errors:
            err_msg = "\n".join(set(errors))
            raise ValueError(
                f"PARAMETER SHADOWING DETECTED. Values must be consistent across side and tier levels:\n{err_msg}"
            )

    def _reset_state(self) -> None:
        """Reset all FSM state for a new run."""
        self._state = TradeState.FLAT
        self._entry_dt = None
        self._entry_price = 0.0
        self._entry_fill = 0.0
        self._atr_at_entry = 0.0
        self._side = 0
        self._tp_price = 0.0
        self._sl_price = 0.0
        self._original_sl_price = 0.0
        self._trailing_rung = 0
        self._bars_held = 0
        self._highest_high = 0.0
        self._lowest_low = float("inf")
        self._multi_tranche = False
        self._tranche_open = []
        self._tranche_closed = []
        self._remaining_lots = 0
        self._trades = []
        self._realized_pnl = 0.0
        self._equity_curve = []
        self._open_positions = []
        self._trade_max_horizon = self.max_horizon
        self._trade_trailing_atr_mult = self.trailing_atr_mult
        self._trade_trailing_sl_atr_offset = self.trailing_sl_atr_offset
        self._trade_trailing_ladder = (
            (self.trailing_atr_mult, self.trailing_sl_atr_offset),
        )
        self._blocked_trades_count = 0

        # Reset mutable engine state
        self._engine_state.position = 0
        self._engine_state.side = 0
        self._engine_state.bars_held = 0
        self._engine_state.open_positions = 0
        self._engine_state.last_exit_bars_ago_long = 9999
        self._engine_state.last_exit_bars_ago_short = 9999

    def _apply_slippage(self, price: float, order_side: str) -> float:
        """Apply 1-tick slippage penalty in the adverse direction.

        Args:
            price: Ideal fill price.
            order_side: 'Buy' or 'Sell'.

        Returns:
            Adjusted fill price (worse for the trader).
        """
        if order_side == "Buy":
            return price + self.slippage_per_side
        return price - self.slippage_per_side

    def _gap_fill_price(
        self, bar_open: float, target_price: float, side: int, is_tp: bool
    ) -> float:
        """Determine fill price accounting for gaps.

        If the bar opens past the target, the fill occurs at the open
        (not the ideal target price) — simulating real gap behavior.

        Args:
            bar_open: Open price of the current bar.
            target_price: The TP or SL barrier price.
            side: +1 for long, -1 for short.
            is_tp: True if this is a take-profit fill.

        Returns:
            Actual fill price (may be worse or better than target).
        """
        if side == 1:  # Long position
            if is_tp:
                # TP: want price to go UP past target
                if bar_open >= target_price:
                    return bar_open  # Gap up past TP — fill at open
                return target_price
            else:
                # SL: price goes DOWN past target
                if bar_open <= target_price:
                    return bar_open  # Gap down past SL — fill at open (worse)
                return target_price
        else:  # Short position
            if is_tp:
                # TP: want price to go DOWN past target
                if bar_open <= target_price:
                    return bar_open
                return target_price
            else:
                # SL: price goes UP past target
                if bar_open >= target_price:
                    return bar_open
                return target_price

    def _close_trade(
        self,
        exit_dt: pd.Timestamp,
        exit_price: float,
        exit_reason: ExitReason,
    ) -> None:
        """Record a completed trade and transition FSM state."""
        if self._multi_tranche:
            # Engine-level exits (e.g. SIGNAL_EXIT) close the REMAINDER of
            # an active ladder — full-lots single-price booking would
            # misprice the already-filled rungs.
            self._close_tranche_remainder(exit_dt, exit_price, exit_reason)
            return
        exit_order_side = "Sell" if self._side == 1 else "Buy"
        exit_fill = self._apply_slippage(exit_price, exit_order_side)

        gross_pnl_price = self._side * (exit_fill - self._entry_fill)
        gross_pnl_dollars = gross_pnl_price * self.contract_multiplier * self._lots
        commission = 2 * self.commission_per_side * self._lots
        net_pnl = gross_pnl_dollars - commission

        record = TradeRecord(
            entry_dt=self._entry_dt,  # type: ignore[arg-type]
            exit_dt=exit_dt,
            entry_price=self._entry_price,
            exit_price=exit_price,
            entry_fill=self._entry_fill,
            exit_fill=exit_fill,
            side=self._side,
            atr_at_entry=self._atr_at_entry,
            exit_reason=exit_reason,
            duration_bars=self._bars_held,
            gross_pnl_dollars=gross_pnl_dollars,
            commission_dollars=commission,
            net_pnl_dollars=net_pnl,
            lots=self._lots,
            initial_tp_price=self._tp_price,
            initial_sl_price=self._original_sl_price,
        )
        self._trades.append(record)
        self._realized_pnl += net_pnl

        # Track exit for strategy-level cooldown logic
        if self._side == 1:
            self._engine_state.last_exit_bars_ago_long = 0
        elif self._side == -1:
            self._engine_state.last_exit_bars_ago_short = 0

        # Notify strategy of exit (for internal state tracking)
        if self._execution_strategy is not None:
            self._execution_strategy.on_exit(self._side, exit_reason, self._bars_held)

        # FSM transition: always transition directly back to FLAT
        self._state = TradeState.FLAT

    def _on_flat(
        self,
        dt: pd.Timestamp,
        bar: pd.Series,
        signal_side: Optional[int],
        atr: float,
        lots: int = 1,
        order: Optional[Order] = None,
    ) -> None:
        """FLAT state: accept valid signals and enter a position.

        Args:
            dt: Bar timestamp.
            bar: OHLCV bar data.
            signal_side: +1 for buy, -1 for sell, None for no signal.
            atr: Current ATR value (should already be side-appropriate).
            lots: Number of contracts for this position.
            order: Optional Order carrying per-trade overrides.
        """
        if signal_side is None or np.isnan(atr) or atr <= 0:
            return

        # Resolve per-trade overrides (Order fields take priority over globals)
        tp_mult = self.tp_atr_mult
        sl_mult = self.sl_atr_mult
        if order is not None:
            if order.tp_atr_mult is not None:
                tp_mult = order.tp_atr_mult
            if order.sl_atr_mult is not None:
                sl_mult = order.sl_atr_mult
            self._trade_max_horizon = (
                order.max_hold_bars if order.max_hold_bars is not None
                else self.max_horizon
            )
            self._trade_trailing_atr_mult = (
                order.trailing_atr_mult if order.trailing_atr_mult is not None
                else self.trailing_atr_mult
            )
        else:
            self._trade_max_horizon = self.max_horizon
            self._trade_trailing_atr_mult = self.trailing_atr_mult

        # Per-side trailing SL offset
        self._trade_trailing_sl_atr_offset = (
            self.trailing_sl_atr_offset_long if signal_side == 1
            else self.trailing_sl_atr_offset_short
        )

        # Per-side trailing ladder. When a ladder is configured, rung 1 is the
        # source of truth; an Order carrying a DIFFERENT trailing override
        # means the config is internally inconsistent — crash loudly.
        _side_ladder = (
            self.trailing_ladder_long if signal_side == 1
            else self.trailing_ladder_short
        )
        if _side_ladder:
            if (
                order is not None
                and order.trailing_atr_mult is not None
                and abs(order.trailing_atr_mult - _side_ladder[0][0]) > 1e-9
            ):
                raise ValueError(
                    f"Order trailing_atr_mult ({order.trailing_atr_mult}) conflicts "
                    f"with trailing_ladder rung 1 activation ({_side_ladder[0][0]})"
                )
            self._trade_trailing_ladder = _side_ladder
        else:
            self._trade_trailing_ladder = (
                (self._trade_trailing_atr_mult, self._trade_trailing_sl_atr_offset),
            )

        self._state = TradeState.IN_POSITION
        self._entry_dt = dt
        self._entry_price = order.override_entry_price if (order is not None and order.override_entry_price is not None) else bar.exec_Close
        self._atr_at_entry = atr
        self._side = signal_side
        self._lots = lots
        self._bars_held = 0
        self._trailing_rung = 0

        entry_order_side = "Buy" if signal_side == 1 else "Sell"
        self._entry_fill = self._apply_slippage(self._entry_price, entry_order_side)

        # Static SL/TP mimic IBKR reality (live_trader.py bracket placement):
        # derived from the slippage-adjusted entry FILL and single-rounded to
        # the CL 0.01 penny grid — round(fill +/- mult*atr, 2).
        if signal_side == 1:
            self._tp_price = round(self._entry_fill + tp_mult * atr, 2)
            self._sl_price = round(self._entry_fill - sl_mult * atr, 2)
            self._highest_high = bar.exec_High
            self._lowest_low = bar.exec_Low
        else:
            self._tp_price = round(self._entry_fill - tp_mult * atr, 2)
            self._sl_price = round(self._entry_fill + sl_mult * atr, 2)
            self._highest_high = bar.exec_High
            self._lowest_low = bar.exec_Low

        self._original_sl_price = self._sl_price

        # Multi-tranche activation (S1): >= 2 rungs AND the shared allocator
        # yields >= 2 nonzero tranches for this position's lots; otherwise
        # the position runs the existing single-exit path bit-for-bit
        # (1-lot ladders collapse to a single tranche and take that path).
        self._multi_tranche = False
        self._tranche_open = []
        self._tranche_closed = []
        self._remaining_lots = lots
        _side_rungs = (
            self.tranche_rungs_long if signal_side == 1
            else self.tranche_rungs_short
        )
        if _side_rungs:
            _alloc = allocate_tranche_lots(lots, [p for p, _ in _side_rungs])
            if len(_alloc) >= 2:
                self._multi_tranche = True
                # Zero-lot rungs are a suffix, so _alloc[i] maps to rung i.
                for _rung_lots, (_pct, _tp_mult) in zip(_alloc, _side_rungs):
                    # Entry-anchored rung TP, priced exactly like the single
                    # TP above: fill-basis + penny-grid rounding.
                    if signal_side == 1:
                        _rung_tp = round(self._entry_fill + _tp_mult * atr, 2)
                    else:
                        _rung_tp = round(self._entry_fill - _tp_mult * atr, 2)
                    self._tranche_open.append((_rung_lots, _rung_tp))

    def _flatten_exit_reason(
        self,
        dt: pd.Timestamp,
        side: int,
        entry_fill: float,
        ref_price: float,
        atr_at_entry: float,
    ) -> Optional[ExitReason]:
        """Which flatten-trigger overlay (if any) should close this position.

        Checks the weekend trigger first, then EOD (their bar-sets are
        disjoint by construction, so order only affects documentation).  A
        trigger fires only when its overlay is enabled, ``dt`` is in its
        precomputed bar-set, and unrealized PnL (evaluated at ``ref_price``)
        is at least its ``profit_atr_mult`` × ATR-at-entry in favor.  Returns
        None on every bar when both overlays are off, so callers are a no-op
        by default.
        """
        if atr_at_entry is None or atr_at_entry <= 0:
            return None
        unrealized_atr: Optional[float] = None  # lazy — only if a set matches
        wf = self._weekend_flatten
        if wf is not None and wf.enabled and dt in self._flatten_bars:
            unrealized_atr = side * (ref_price - entry_fill) / atr_at_entry
            if unrealized_atr >= wf.profit_atr_mult:
                return ExitReason.WEEKEND_FLATTEN
        ef = self._eod_flatten
        if ef is not None and ef.enabled and dt in self._eod_flatten_bars:
            if unrealized_atr is None:
                unrealized_atr = side * (ref_price - entry_fill) / atr_at_entry
            if unrealized_atr >= ef.profit_atr_mult:
                return ExitReason.EOD_FLATTEN
        return None

    def _on_in_position(self, dt: pd.Timestamp, bar_open: float,
                        bar_high: float, bar_low: float) -> None:
        """IN_POSITION state: manage an active trade (pessimistic).

        Checks:
        1. Evaluate both TP and SL — if BOTH breach, SL wins
        2. Time-barrier exit (only if no TP/SL fill this bar)
        3. Trailing stop upgrade to breakeven

        Precedence mirrors IBKR reality (B(b), human-authorized 2026-07-03):
        resting TP/SL orders fill INTRABAR; the live time-barrier check runs
        at bar close only if still in position — so on the barrier bar a
        TP/SL breach wins over TIME_BARRIER.
        """
        self._bars_held += 1

        # Track extremes since entry
        self._highest_high = max(self._highest_high, bar_high)
        self._lowest_low = min(self._lowest_low, bar_low)

        # Multi-tranche ladders branch off here; the single path below is
        # untouched (S5 identity fence).
        if self._multi_tranche:
            self._on_in_position_tranche(dt, bar_open, bar_high, bar_low)
            return

        # 1. Evaluate BOTH TP and SL FIRST — pessimistic: SL wins on same-bar
        if self._side == 1:
            tp_hit = bar_high >= self._tp_price
            sl_hit = bar_low <= self._sl_price
        else:
            tp_hit = bar_low <= self._tp_price
            sl_hit = bar_high >= self._sl_price

        if tp_hit and sl_hit:
            # Both barriers breached — assume worst case (SL)
            exit_price = self._gap_fill_price(
                bar_open, self._sl_price, self._side, is_tp=False
            )
            reason = ExitReason.TRAILING_BE if self._trailing_rung > 0 else ExitReason.SL
            self._close_trade(dt, exit_price, reason)
            return

        if sl_hit:
            exit_price = self._gap_fill_price(
                bar_open, self._sl_price, self._side, is_tp=False
            )
            reason = ExitReason.TRAILING_BE if self._trailing_rung > 0 else ExitReason.SL
            self._close_trade(dt, exit_price, reason)
            return

        if tp_hit:
            exit_price = self._gap_fill_price(
                bar_open, self._tp_price, self._side, is_tp=True
            )
            self._close_trade(dt, exit_price, ExitReason.TP)
            return

        # 2. Time-barrier exit (no TP/SL fill this bar; exit at bar open)
        if self._bars_held > self._trade_max_horizon:
            self._close_trade(dt, bar_open, ExitReason.TIME_BARRIER)
            return

        # 2b. Flatten triggers — weekend / EOD (optional overlays; no-op when
        #     disabled). Reached only if the position survived TP/SL and the
        #     time barrier this bar, so it genuinely alters the trade. Fill
        #     mirrors TIME_BARRIER (bar open).
        _flat_reason = self._flatten_exit_reason(
            dt, self._side, self._entry_fill, bar_open, self._atr_at_entry
        )
        if _flat_reason is not None:
            self._close_trade(dt, bar_open, _flat_reason)
            return

        # 3. Trailing ladder ratchet: after the extreme-since-entry crosses a
        #    rung's activation (+N×ATR in favor), move the SL to that rung's
        #    lock (entry ± lock×ATR; 0 = breakeven). A single bar may advance
        #    multiple rungs; the moved stop is only effective from the NEXT
        #    bar (this block runs after the exit checks above). Legacy configs
        #    are a 1-rung ladder — byte-identical to the old one-shot latch.
        _ladder = self._trade_trailing_ladder
        while self._trailing_rung < len(_ladder):
            _act, _lock = _ladder[self._trailing_rung]
            if self._side == 1:
                if self._highest_high < (
                    self._entry_price + _act * self._atr_at_entry
                ):
                    break
                self._sl_price = (
                    self._entry_price + _lock * self._atr_at_entry
                )
            else:
                if self._lowest_low > (
                    self._entry_price - _act * self._atr_at_entry
                ):
                    break
                self._sl_price = (
                    self._entry_price - _lock * self._atr_at_entry
                )
            self._trailing_rung += 1

    # -------------------------------------------------------------------
    # Multi-tranche (scale-out) helpers — single-position mode only
    # -------------------------------------------------------------------

    def _on_in_position_tranche(self, dt: pd.Timestamp, bar_open: float,
                                bar_high: float, bar_low: float) -> None:
        """Per-bar management of an active multi-tranche ladder (S2).

        Mirrors _on_in_position precedence: SL is pessimistic (closes the
        whole remainder even when rungs also breached — unfilled rungs are
        void); else every breached unfilled rung fills at its own gap-aware
        TP price; ANY fill consumes the bar (barrier/flatten/ratchet resume
        next bar); barrier/flatten close the remainder at bar open; the
        trailing ratchet moves the one SL guarding the remainder.
        """
        if self._side == 1:
            sl_hit = bar_low <= self._sl_price
        else:
            sl_hit = bar_high >= self._sl_price

        if sl_hit:
            exit_price = self._gap_fill_price(
                bar_open, self._sl_price, self._side, is_tp=False
            )
            reason = ExitReason.TRAILING_BE if self._trailing_rung > 0 else ExitReason.SL
            self._close_tranche_remainder(dt, exit_price, reason)
            return

        filled_this_bar = False
        surviving: list[tuple[int, float]] = []
        for rung_lots, rung_tp in self._tranche_open:
            if self._side == 1:
                tp_hit = bar_high >= rung_tp
            else:
                tp_hit = bar_low <= rung_tp
            if tp_hit:
                fill_price = self._gap_fill_price(
                    bar_open, rung_tp, self._side, is_tp=True
                )
                self._book_tranche_close(
                    dt, rung_lots, fill_price, ExitReason.TP
                )
                filled_this_bar = True
            else:
                surviving.append((rung_lots, rung_tp))
        self._tranche_open = surviving

        if self._remaining_lots == 0:
            self._finalize_tranche_trade(dt, ExitReason.TP)
            return
        if filled_this_bar:
            # Any fill consumes the bar (the single path returns after a
            # fill): barrier, flatten overlays, and ratchet resume next bar.
            return

        if self._bars_held > self._trade_max_horizon:
            self._close_tranche_remainder(dt, bar_open, ExitReason.TIME_BARRIER)
            return

        _flat_reason = self._flatten_exit_reason(
            dt, self._side, self._entry_fill, bar_open, self._atr_at_entry
        )
        if _flat_reason is not None:
            self._close_tranche_remainder(dt, bar_open, _flat_reason)
            return

        # Trailing ladder ratchet — unchanged math (see _on_in_position
        # step 3); moves the one SL protecting the remainder.
        _ladder = self._trade_trailing_ladder
        while self._trailing_rung < len(_ladder):
            _act, _lock = _ladder[self._trailing_rung]
            if self._side == 1:
                if self._highest_high < (
                    self._entry_price + _act * self._atr_at_entry
                ):
                    break
                self._sl_price = (
                    self._entry_price + _lock * self._atr_at_entry
                )
            else:
                if self._lowest_low > (
                    self._entry_price - _act * self._atr_at_entry
                ):
                    break
                self._sl_price = (
                    self._entry_price - _lock * self._atr_at_entry
                )
            self._trailing_rung += 1

    def _book_tranche_close(
        self,
        exit_dt: pd.Timestamp,
        lots: int,
        exit_price: float,
        reason: ExitReason,
    ) -> None:
        """Accumulate one tranche close (slippage applied per tranche
        exactly like _close_trade)."""
        exit_order_side = "Sell" if self._side == 1 else "Buy"
        exit_fill = self._apply_slippage(exit_price, exit_order_side)
        self._tranche_closed.append({
            "lots": lots,
            "exit_dt": exit_dt,
            "exit_price": exit_price,
            "exit_fill": exit_fill,
            "reason": reason,
        })
        self._remaining_lots -= lots

    def _close_tranche_remainder(
        self,
        exit_dt: pd.Timestamp,
        exit_price: float,
        exit_reason: ExitReason,
    ) -> None:
        """Close the entire remainder at one price and finalize the trade.
        Unfilled rungs are void (S2 SL pessimism / overlay closes)."""
        self._book_tranche_close(
            exit_dt, self._remaining_lots, exit_price, exit_reason
        )
        self._tranche_open = []
        self._finalize_tranche_trade(exit_dt, exit_reason)

    def _finalize_tranche_trade(
        self, exit_dt: pd.Timestamp, exit_reason: ExitReason
    ) -> None:
        """Emit the ONE TradeRecord for a completed multi-tranche position
        (S3): per-tranche gross, today's commission total, lots-weighted
        exit price/fill, final-event exit_dt/reason; on_exit/cooldown fire
        once here with the final reason."""
        total_lots = self._lots
        gross_pnl_dollars = 0.0
        weighted_price = 0.0
        weighted_fill = 0.0
        for tranche in self._tranche_closed:
            gross_pnl_dollars += (
                self._side * (tranche["exit_fill"] - self._entry_fill)
                * self.contract_multiplier * tranche["lots"]
            )
            weighted_price += tranche["exit_price"] * tranche["lots"]
            weighted_fill += tranche["exit_fill"] * tranche["lots"]
        commission = 2 * self.commission_per_side * total_lots
        net_pnl = gross_pnl_dollars - commission

        record = TradeRecord(
            entry_dt=self._entry_dt,  # type: ignore[arg-type]
            exit_dt=exit_dt,
            entry_price=self._entry_price,
            exit_price=weighted_price / total_lots,
            entry_fill=self._entry_fill,
            exit_fill=weighted_fill / total_lots,
            side=self._side,
            atr_at_entry=self._atr_at_entry,
            exit_reason=exit_reason,
            duration_bars=self._bars_held,
            gross_pnl_dollars=gross_pnl_dollars,
            commission_dollars=commission,
            net_pnl_dollars=net_pnl,
            lots=total_lots,
            initial_tp_price=self._tp_price,
            initial_sl_price=self._original_sl_price,
            tranche_exits=(
                list(self._tranche_closed)
                if len(self._tranche_closed) >= 2 else None
            ),
        )
        self._trades.append(record)
        self._realized_pnl += net_pnl

        # Track exit for strategy-level cooldown logic
        if self._side == 1:
            self._engine_state.last_exit_bars_ago_long = 0
        elif self._side == -1:
            self._engine_state.last_exit_bars_ago_short = 0

        # Notify strategy of exit (once, with the final reason)
        if self._execution_strategy is not None:
            self._execution_strategy.on_exit(
                self._side, exit_reason, self._bars_held
            )

        self._multi_tranche = False
        self._tranche_open = []
        self._tranche_closed = []
        self._remaining_lots = 0

        # FSM transition: always transition directly back to FLAT
        self._state = TradeState.FLAT

    # -------------------------------------------------------------------
    # Concurrent-mode helpers
    # -------------------------------------------------------------------

    def _open_new_position(
        self,
        dt: pd.Timestamp,
        bar: pd.Series,
        signal_side: int,
        atr: float,
        lots: int = 1,
        order: Optional[Order] = None,
    ) -> None:
        """Open a new position and add it to the open-positions list."""
        entry_price = order.override_entry_price if (order is not None and order.override_entry_price is not None) else bar.exec_Close
        entry_order_side = "Buy" if signal_side == 1 else "Sell"
        entry_fill = self._apply_slippage(entry_price, entry_order_side)

        # Resolve per-trade overrides
        tp_mult = self.tp_atr_mult
        sl_mult = self.sl_atr_mult
        pos_max_horizon: Optional[int] = None
        pos_trailing_atr_mult: Optional[float] = None
        if order is not None:
            if order.tp_atr_mult is not None:
                tp_mult = order.tp_atr_mult
            if order.sl_atr_mult is not None:
                sl_mult = order.sl_atr_mult
            if order.max_hold_bars is not None:
                pos_max_horizon = order.max_hold_bars
            if order.trailing_atr_mult is not None:
                pos_trailing_atr_mult = order.trailing_atr_mult

        # Resolve the trailing ladder (same rules as _on_flat): a configured
        # side ladder wins and must agree with any Order trailing override;
        # otherwise build the legacy 1-rung ladder from the resolved values.
        _side_ladder = (
            self.trailing_ladder_long if signal_side == 1
            else self.trailing_ladder_short
        )
        _side_offset = (
            self.trailing_sl_atr_offset_long if signal_side == 1
            else self.trailing_sl_atr_offset_short
        )
        if _side_ladder:
            if (
                pos_trailing_atr_mult is not None
                and abs(pos_trailing_atr_mult - _side_ladder[0][0]) > 1e-9
            ):
                raise ValueError(
                    f"Order trailing_atr_mult ({pos_trailing_atr_mult}) conflicts "
                    f"with trailing_ladder rung 1 activation ({_side_ladder[0][0]})"
                )
            pos_ladder = _side_ladder
        else:
            _act = (
                pos_trailing_atr_mult if pos_trailing_atr_mult is not None
                else self.trailing_atr_mult
            )
            pos_ladder = ((_act, _side_offset),)

        # Static SL/TP mimic IBKR reality: fill-basis + penny-grid rounding
        # (same derivation as _on_flat).
        if signal_side == 1:
            tp_price = round(entry_fill + tp_mult * atr, 2)
            sl_price = round(entry_fill - sl_mult * atr, 2)
        else:
            tp_price = round(entry_fill - tp_mult * atr, 2)
            sl_price = round(entry_fill + sl_mult * atr, 2)

        pos = _OpenPosition(
            entry_dt=dt,
            entry_price=entry_price,
            entry_fill=entry_fill,
            atr_at_entry=atr,
            side=signal_side,
            tp_price=tp_price,
            sl_price=sl_price,
            original_sl_price=sl_price,
            highest_high=bar.exec_High,
            lowest_low=bar.exec_Low,
            lots=lots,
            pos_max_horizon=pos_max_horizon,
            pos_trailing_ladder=pos_ladder,
        )
        self._open_positions.append(pos)

    def _check_position(
        self,
        pos: _OpenPosition,
        dt: pd.Timestamp,
        bar_open: float,
        bar_high: float,
        bar_low: float,
    ) -> Optional[TradeRecord]:
        """Check an open position for exit conditions (pessimistic).

        If both TP and SL breach on the same bar, SL wins.
        Returns a TradeRecord if the position closed, else None.
        """
        pos.bars_held += 1

        pos.highest_high = max(pos.highest_high, bar_high)
        pos.lowest_low = min(pos.lowest_low, bar_low)

        exit_price: Optional[float] = None
        exit_reason: Optional[ExitReason] = None

        # 1. Evaluate BOTH TP and SL FIRST — pessimistic: SL wins on conflict.
        #    Intrabar fills beat the bar-close time barrier (B(b), 2026-07-03).
        if pos.side == 1:
            tp_hit = bar_high >= pos.tp_price
            sl_hit = bar_low <= pos.sl_price
        else:
            tp_hit = bar_low <= pos.tp_price
            sl_hit = bar_high >= pos.sl_price

        if tp_hit and sl_hit:
            exit_price = self._gap_fill_price(
                bar_open, pos.sl_price, pos.side, is_tp=False
            )
            exit_reason = (
                ExitReason.TRAILING_BE if pos.trailing_rung > 0
                else ExitReason.SL
            )
        elif sl_hit:
            exit_price = self._gap_fill_price(
                bar_open, pos.sl_price, pos.side, is_tp=False
            )
            exit_reason = (
                ExitReason.TRAILING_BE if pos.trailing_rung > 0
                else ExitReason.SL
            )
        elif tp_hit:
            exit_price = self._gap_fill_price(
                bar_open, pos.tp_price, pos.side, is_tp=True
            )
            exit_reason = ExitReason.TP

        # 2. Time barrier (only if no TP/SL fill; per-position override if set)
        if exit_reason is None:
            effective_horizon = pos.pos_max_horizon if pos.pos_max_horizon is not None else self.max_horizon
            if pos.bars_held > effective_horizon:
                exit_price = bar_open
                exit_reason = ExitReason.TIME_BARRIER

        # 2b. Flatten triggers — weekend / EOD (optional overlays; no-op when
        #     disabled). Only if still open after TP/SL and the time barrier
        #     this bar.
        if exit_reason is None:
            _flat_reason = self._flatten_exit_reason(
                dt, pos.side, pos.entry_fill, bar_open, pos.atr_at_entry
            )
            if _flat_reason is not None:
                exit_price = bar_open
                exit_reason = _flat_reason

        # 3. Trailing ladder ratchet (see _on_in_position — same semantics:
        #    advance after exit checks, moved stop effective next bar)
        if exit_reason is None:
            _ladder = pos.pos_trailing_ladder
            while pos.trailing_rung < len(_ladder):
                _act, _lock = _ladder[pos.trailing_rung]
                if pos.side == 1:
                    if pos.highest_high < (
                        pos.entry_price + _act * pos.atr_at_entry
                    ):
                        break
                    pos.sl_price = (
                        pos.entry_price + _lock * pos.atr_at_entry
                    )
                else:
                    if pos.lowest_low > (
                        pos.entry_price - _act * pos.atr_at_entry
                    ):
                        break
                    pos.sl_price = (
                        pos.entry_price - _lock * pos.atr_at_entry
                    )
                pos.trailing_rung += 1

        if exit_reason is not None and exit_price is not None:
            exit_order_side = "Sell" if pos.side == 1 else "Buy"
            exit_fill = self._apply_slippage(exit_price, exit_order_side)
            gross_pnl_price = pos.side * (exit_fill - pos.entry_fill)
            gross_pnl_dollars = gross_pnl_price * self.contract_multiplier * pos.lots
            commission = 2 * self.commission_per_side * pos.lots
            net_pnl = gross_pnl_dollars - commission

            return TradeRecord(
                entry_dt=pos.entry_dt,
                exit_dt=dt,
                entry_price=pos.entry_price,
                exit_price=exit_price,
                entry_fill=pos.entry_fill,
                exit_fill=exit_fill,
                side=pos.side,
                atr_at_entry=pos.atr_at_entry,
                exit_reason=exit_reason,
                duration_bars=pos.bars_held,
                gross_pnl_dollars=gross_pnl_dollars,
                commission_dollars=commission,
                net_pnl_dollars=net_pnl,
                lots=pos.lots,
                initial_tp_price=pos.tp_price,
                initial_sl_price=pos.original_sl_price,
            )

        return None

    # -------------------------------------------------------------------
    # Main run method
    # -------------------------------------------------------------------

    def run(
        self,
        signals_df: pd.DataFrame,
        ohlcv_df: pd.DataFrame,
        ohlcv_exec_df: pd.DataFrame | None = None,
        *,
        label: str = "",
    ) -> BacktestResult:
        """Run the backtest bar-by-bar over the OHLCV data.

        Dispatches to single-position FSM or concurrent mode based on
        ``self.allow_concurrent``.

        Args:
            signals_df: DataFrame indexed by DateTime with signal columns.
                        Must contain 'side' column (+1/-1) for signal bars
                        OR 'prob_Buy' column for probability-based filtering.
            ohlcv_df: Full OHLCV DataFrame indexed by DateTime with columns:
                      Open, High, Low, Close, Volume.
            label: Human-readable label for this run (e.g., "Historical").

        Returns:
            BacktestResult with all completed trades and aggregate metrics.
        """
        self._reset_state()

        # Compute per-side ATR on the OHLCV data.
        # Fast path: if ATR_{period} columns were pre-stamped by attach_atr_cache()
        # in strategy_optimizer.py, use them directly and skip the rolling-mean
        # recomputation.  This eliminates the dominant per-trial overhead in the
        # Optuna hot loop.  Falls back to the legacy computation for any period
        # not found in the DataFrame (zero fidelity loss — same formula).
        ohlcv = ohlcv_df.copy()
        long_col  = f"ATR_{self.atr_period_long}"
        short_col = f"ATR_{self.atr_period_short}"
        long_cached  = long_col  in ohlcv_df.columns
        short_cached = short_col in ohlcv_df.columns

        import pandas_ta as _ta
        if not long_cached:
            ohlcv["atr_long_"] = ohlcv.ta.atr(length=self.atr_period_long).values
        else:
            ohlcv["atr_long_"] = ohlcv_df[long_col].values
            
        if not short_cached:
            ohlcv["atr_short_"] = ohlcv.ta.atr(length=self.atr_period_short).values
        else:
            ohlcv["atr_short_"] = ohlcv_df[short_col].values
            
        # Legacy column: used by legacy loop methods and as default
        ohlcv["atr_"] = ohlcv["atr_short_"]

        # Stamp execution columns for the trade wallet
        if ohlcv_exec_df is not None:
            exec_aligned = ohlcv_exec_df.reindex(ohlcv.index)
            ohlcv["exec_Open"]  = exec_aligned["Open"].values
            ohlcv["exec_High"]  = exec_aligned["High"].values
            ohlcv["exec_Low"]   = exec_aligned["Low"].values
            ohlcv["exec_Close"] = exec_aligned["Close"].values

            # Compute execution ATR from raw prices for TP/SL sizing
            ohlcv["exec_atr_"] = exec_aligned.ta.atr(length=self.atr_period).values
            ohlcv["exec_atr_long_"]  = exec_aligned.ta.atr(length=self.atr_period_long).values
            ohlcv["exec_atr_short_"] = exec_aligned.ta.atr(length=self.atr_period_short).values
        else:
            # Fallback: use same OHLCV for both brain and wallet
            ohlcv["exec_Open"]  = ohlcv["Open"].values
            ohlcv["exec_High"]  = ohlcv["High"].values
            ohlcv["exec_Low"]   = ohlcv["Low"].values
            ohlcv["exec_Close"] = ohlcv["Close"].values
            ohlcv["exec_atr_"]       = ohlcv["atr_"].values
            ohlcv["exec_atr_long_"]  = ohlcv["atr_long_"].values
            ohlcv["exec_atr_short_"] = ohlcv["atr_short_"].values

        # Precompute flatten-trigger bars (optional overlays; empty = off).
        # Derived purely from the data's own bar spacing: no calendar, no
        # lookahead, symbol-agnostic.
        #   - weekend bar: last bar before a gap >= weekend.min_gap_hours
        #     (~49h Fri->Mon; catches holiday-extended weekends too).
        #   - EOD bar: last bar before a gap in [eod.min_gap_hours,
        #     weekend threshold) — the daily session halt (2h on CME hourly
        #     bars) and holiday early-closes.  Disjoint from the weekend band
        #     by construction so WEEKEND/EOD attribution never overlaps.
        self._flatten_bars = set()
        self._eod_flatten_bars = set()
        wf = self._weekend_flatten
        ef = self._eod_flatten
        weekend_on = wf is not None and wf.enabled
        eod_on = ef is not None and ef.enabled
        if weekend_on or eod_on:
            from src.live_execution.strategy_config import (
                _DEFAULT_WEEKEND_MIN_GAP_HOURS,
            )
            weekend_threshold = (
                wf.min_gap_hours if wf is not None
                else _DEFAULT_WEEKEND_MIN_GAP_HOURS
            )
            if eod_on and ef.min_gap_hours >= weekend_threshold:
                raise ValueError(
                    f"eod_flatten.min_gap_hours ({ef.min_gap_hours}) must be "
                    f"< the weekend gap threshold ({weekend_threshold}); the "
                    f"EOD band would be empty or swallow weekend gaps."
                )
            idx = ohlcv.index
            if len(idx) >= 2:
                deltas = idx[1:] - idx[:-1]  # gap from bar i to bar i+1
                if weekend_on:
                    wkd_mask = deltas >= pd.Timedelta(hours=wf.min_gap_hours)
                    self._flatten_bars = {
                        pd.Timestamp(t) for t in idx[:-1][wkd_mask]
                    }
                if eod_on:
                    eod_mask = (
                        (deltas >= pd.Timedelta(hours=ef.min_gap_hours))
                        & (deltas < pd.Timedelta(hours=weekend_threshold))
                    )
                    self._eod_flatten_bars = {
                        pd.Timestamp(t) for t in idx[:-1][eod_mask]
                    }

        # Build signal lookup — which bars have a trade signal
        #
        # Two code paths:
        #   1. Strategy-aware: delegate to execution strategy on_bar()
        #   2. Legacy: build signal_sides dict from columns (backward compat)
        #
        if self._execution_strategy is not None:
            # Strategy-aware path: build prob lookup dicts
            prob_buy_lookup: dict[pd.Timestamp, float] = {}
            prob_sell_lookup: dict[pd.Timestamp, float] = {}

            buy_col = _resolve_prob_column(signals_df, "buy")
            sell_col = _resolve_prob_column(signals_df, "sell")
            if buy_col:
                for dt_idx in signals_df.index:
                    prob_buy_lookup[pd.Timestamp(dt_idx)] = float(
                        signals_df.at[dt_idx, buy_col]
                    )
            if sell_col:
                for dt_idx in signals_df.index:
                    prob_sell_lookup[pd.Timestamp(dt_idx)] = float(
                        signals_df.at[dt_idx, sell_col]
                    )
            # Legacy column fallback: if only 'side' column, map to probs
            if "side" in signals_df.columns and not prob_buy_lookup and not prob_sell_lookup:
                for dt_idx in signals_df.index:
                    s = int(signals_df.at[dt_idx, "side"])
                    if s == 1:
                        prob_buy_lookup[pd.Timestamp(dt_idx)] = 1.0
                    elif s == -1:
                        prob_sell_lookup[pd.Timestamp(dt_idx)] = 1.0
            # Predicted column fallback
            if "Predicted" in signals_df.columns and not prob_buy_lookup:
                mask = signals_df["Predicted"] == 1
                for dt_idx in signals_df[mask].index:
                    prob_buy_lookup[pd.Timestamp(dt_idx)] = 1.0

            if self.allow_concurrent:
                self._run_concurrent_strategy(
                    ohlcv, prob_buy_lookup, prob_sell_lookup
                )
            else:
                self._run_single_strategy(
                    ohlcv, prob_buy_lookup, prob_sell_lookup
                )
        else:
            # Legacy path: build signal_sides dict
            signal_sides: dict[pd.Timestamp, int] = {}

            if "side" in signals_df.columns:
                for dt_idx in signals_df.index:
                    ts = pd.Timestamp(dt_idx)
                    signal_sides[ts] = int(signals_df.at[dt_idx, "side"])
            elif _resolve_prob_column(signals_df, "buy"):
                _buy_col = _resolve_prob_column(signals_df, "buy")
                mask = signals_df[_buy_col] >= self.prob_threshold
                for dt_idx in signals_df[mask].index:
                    signal_sides[pd.Timestamp(dt_idx)] = 1
            elif "Predicted" in signals_df.columns:
                mask = signals_df["Predicted"] == 1
                for dt_idx in signals_df[mask].index:
                    signal_sides[pd.Timestamp(dt_idx)] = 1

            if self.allow_concurrent:
                self._run_concurrent(ohlcv, signal_sides)
            else:
                self._run_single(ohlcv, signal_sides)

        return BacktestResult(
            trades=self._trades,
            equity_curve=self._equity_curve,
            label=label,
            start_dt=signals_df.index.min() if not signals_df.empty else (ohlcv.index.min() if not ohlcv.empty else None),
            end_dt=signals_df.index.max() if not signals_df.empty else (ohlcv.index.max() if not ohlcv.empty else None),
            blocked_trades_count=self._blocked_trades_count,
        )

    def _floating_pnl_single(self, close: float) -> float:
        """Floating PnL of the single open position."""
        if self._state != TradeState.IN_POSITION:
            return 0.0
        return self._side * (close - self._entry_fill) * self.contract_multiplier

    def _floating_pnl_concurrent(self, close: float) -> float:
        """Total floating PnL of all open concurrent positions."""
        total = 0.0
        for pos in self._open_positions:
            total += pos.side * (close - pos.entry_fill) * self.contract_multiplier
        return total

    def _run_single(
        self,
        ohlcv: pd.DataFrame,
        signal_sides: dict[pd.Timestamp, int],
    ) -> None:
        """Single-position FSM loop using itertuples for speed."""
        for row in ohlcv.itertuples():
            ts = pd.Timestamp(row.Index)
            atr = row.atr_

            self._engine_state.last_exit_bars_ago_long += 1
            self._engine_state.last_exit_bars_ago_short += 1

            if self._state == TradeState.FLAT:
                sig = signal_sides.get(ts)
                # Legacy path: select ATR by signal side
                if sig == 1:
                    side_atr = row.exec_atr_long_
                elif sig == -1:
                    side_atr = row.exec_atr_short_
                else:
                    side_atr = row.exec_atr_
                self._on_flat(ts, row, sig, side_atr)

            elif self._state == TradeState.IN_POSITION:
                self._on_in_position(ts, row.exec_Open, row.exec_High, row.exec_Low)

            # Record equity: realized + floating
            self._equity_curve.append(
                self._realized_pnl + self._floating_pnl_single(row.exec_Close)
            )

    def _run_concurrent(
        self,
        ohlcv: pd.DataFrame,
        signal_sides: dict[pd.Timestamp, int],
    ) -> None:
        """Concurrent multi-position loop using itertuples for speed.

        On each bar:
        1. Check all open positions for exits (TP/SL/trailing/time)
        2. If a signal is present and we haven't hit max_concurrent, open new
        3. Record equity (realized + floating)
        """
        for row in ohlcv.itertuples():
            ts = pd.Timestamp(row.Index)
            atr = row.atr_

            # 0. Increment global bars since exit
            self._engine_state.last_exit_bars_ago_long += 1
            self._engine_state.last_exit_bars_ago_short += 1

            # 1. Check existing positions for exits
            surviving: list[_OpenPosition] = []
            for pos in self._open_positions:
                record = self._check_position(pos, ts, row.exec_Open, row.exec_High, row.exec_Low)
                if record is not None:
                    self._trades.append(record)
                    self._realized_pnl += record.net_pnl_dollars
                else:
                    surviving.append(pos)
            self._open_positions = surviving

            # 2. Accept new signals if room
            sig = signal_sides.get(ts)
            if (
                sig is not None
                and len(self._open_positions) < self.max_concurrent
            ):
                # Select ATR by signal side
                side_atr = row.exec_atr_long_ if sig == 1 else row.exec_atr_short_
                if not (np.isnan(side_atr) if isinstance(side_atr, float) else False) and side_atr > 0:
                    self._open_new_position(ts, row, sig, side_atr)

            # 3. Record equity: realized + floating
            self._equity_curve.append(
                self._realized_pnl + self._floating_pnl_concurrent(row.exec_Close)
            )

    # -------------------------------------------------------------------
    # Strategy-aware loop methods
    # -------------------------------------------------------------------

    def _update_engine_state(self, exec_close: Optional[float] = None) -> None:
        """Sync the mutable EngineState with FSM state (single mode).

        ``exec_close`` (raw-basis bar close) feeds the exec-basis position
        economics: entry_price (= entry fill) and floating_pnl_points
        (= side * (exec_close - entry_fill), gross).  Both are None when flat
        or when exec_close is not supplied.
        """
        es = self._engine_state
        if self._state == TradeState.IN_POSITION:
            es.position = 1
            es.side = self._side
            es.bars_held = self._bars_held
            es.entry_price = self._entry_fill
            es.floating_pnl_points = (
                self._side * (exec_close - self._entry_fill)
                if exec_close is not None else None
            )
        else:
            es.position = 0
            es.side = 0
            es.bars_held = 0
            es.entry_price = None
            es.floating_pnl_points = None
        es.open_positions = 1 if self._state == TradeState.IN_POSITION else 0

    def _run_single_strategy(
        self,
        ohlcv: pd.DataFrame,
        prob_buy_lookup: dict[pd.Timestamp, float],
        prob_sell_lookup: dict[pd.Timestamp, float],
    ) -> None:
        """Single-position FSM loop with execution strategy delegation."""
        strategy = self._execution_strategy
        assert strategy is not None

        for row in ohlcv.itertuples():
            ts = pd.Timestamp(row.Index)
            atr = row.atr_
            atr_long = row.atr_long_
            atr_short = row.atr_short_

            self._engine_state.last_exit_bars_ago_long += 1
            self._engine_state.last_exit_bars_ago_short += 1

            if self._state == TradeState.IN_POSITION:
                self._on_in_position(ts, row.exec_Open, row.exec_High, row.exec_Low)

            # Update engine state for strategy (exec close feeds the
            # exec-basis floating-PnL fields)
            self._update_engine_state(row.exec_Close)

            # Get probabilities for this bar
            pb = prob_buy_lookup.get(ts, 0.0)
            ps = prob_sell_lookup.get(ts, 0.0)

            # Ask strategy what to do
            orders = strategy.on_bar(
                ts, row.Open, row.High, row.Low, row.Close,
                atr, pb, ps, self._engine_state,
            )

            for order in orders:
                if order.action == "EXIT" and self._state == TradeState.IN_POSITION:
                    self._close_trade(ts, row.exec_Close, ExitReason.SIGNAL_EXIT)
                    self._update_engine_state(row.exec_Close)
                    break

            # Dispatch orders to existing FSM entry point
            if self._state == TradeState.FLAT:
                has_entry = any(order.action in ("BUY", "SELL") for order in orders)
                # ExecutionGuard: block new entries during toxic periods.
                # Config hours express blocked FILL times. The backtest fills
                # at bar.Close (= bar_start + 1h for hourly bars), so shift
                # the guard timestamp forward by 1h to match fill-time semantics.
                fill_ts = ts + pd.Timedelta(hours=1)
                if self._execution_guard and not self._execution_guard.is_entry_allowed(fill_ts):
                    if has_entry:
                        self._blocked_trades_count += 1
                    pass  # Guard blocked — skip entry
                else:
                    for order in orders:
                        if order.action in ("BUY", "SELL"):
                            sig = order.side
                            # Select ATR by trade side
                            side_atr = row.exec_atr_long_ if sig == 1 else row.exec_atr_short_
                            self._on_flat(ts, row, sig, side_atr, lots=order.lots, order=order)
                            break  # single-position: only one entry per bar

            # Record equity: realized + floating
            self._equity_curve.append(
                self._realized_pnl + self._floating_pnl_single(row.exec_Close)
            )

    def _run_concurrent_strategy(
        self,
        ohlcv: pd.DataFrame,
        prob_buy_lookup: dict[pd.Timestamp, float],
        prob_sell_lookup: dict[pd.Timestamp, float],
    ) -> None:
        """Concurrent multi-position loop with execution strategy delegation."""
        strategy = self._execution_strategy
        assert strategy is not None

        for row in ohlcv.itertuples():
            ts = pd.Timestamp(row.Index)
            atr = row.atr_
            atr_long = row.atr_long_
            atr_short = row.atr_short_

            # 1. Check existing positions for exits
            surviving: list[_OpenPosition] = []
            for pos in self._open_positions:
                record = self._check_position(pos, ts, row.exec_Open, row.exec_High, row.exec_Low)
                if record is not None:
                    self._trades.append(record)
                    self._realized_pnl += record.net_pnl_dollars
                    # Notify strategy of exit (for internal state tracking)
                    strategy.on_exit(pos.side, record.exit_reason, record.duration_bars)
                else:
                    surviving.append(pos)
            self._open_positions = surviving

            # 2. Update engine state for strategy.  Exec-basis economics come
            # from the FIRST open position (concurrent mode carries one
            # side at a time in practice; single mode is the fleet default).
            self._engine_state.open_positions = len(self._open_positions)
            if self._open_positions:
                self._engine_state.position = 1
                self._engine_state.side = self._open_positions[0].side
                _p0 = self._open_positions[0]
                self._engine_state.entry_price = _p0.entry_fill
                self._engine_state.floating_pnl_points = (
                    _p0.side * (row.exec_Close - _p0.entry_fill)
                )
            else:
                self._engine_state.position = 0
                self._engine_state.side = 0
                self._engine_state.entry_price = None
                self._engine_state.floating_pnl_points = None

            # 3. Ask strategy what to do
            pb = prob_buy_lookup.get(ts, 0.0)
            ps = prob_sell_lookup.get(ts, 0.0)
            orders = strategy.on_bar(
                ts, row.Open, row.High, row.Low, row.Close,
                atr, pb, ps, self._engine_state,
            )

            # 4. Dispatch orders (ExecutionGuard: block new entries during toxic periods)
            # Config hours express blocked FILL times — shift by +1h (see single-position comment).
            fill_ts = ts + pd.Timedelta(hours=1)
            is_allowed = not self._execution_guard or self._execution_guard.is_entry_allowed(fill_ts)
            for order in orders:
                if order.action in ("BUY", "SELL"):
                    if not is_allowed:
                        if len(self._open_positions) < self.max_concurrent:
                            self._blocked_trades_count += 1
                        continue
                    # Select ATR by trade side
                    side_atr = row.exec_atr_long_ if order.side == 1 else row.exec_atr_short_
                    if (
                        not (np.isnan(side_atr) if isinstance(side_atr, float) else False)
                        and side_atr > 0
                        and len(self._open_positions) < self.max_concurrent
                    ):
                        self._open_new_position(ts, row, order.side, side_atr, lots=order.lots, order=order)

            # 5. Record equity: realized + floating
            self._equity_curve.append(
                self._realized_pnl + self._floating_pnl_concurrent(row.exec_Close)
            )


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _safe_wr(wins: int, total: int) -> str:
    """Format win rate, returning '-' if no trades."""
    if total == 0:
        return "   -  "
    return f"{wins / total:>5.1%} "


def _safe_pf(trades: list[TradeRecord]) -> float:
    """Calculate profit factor from a list of trades."""
    gross_profit = sum(t.net_pnl_dollars for t in trades if t.net_pnl_dollars > 0)
    gross_loss = abs(sum(t.net_pnl_dollars for t in trades if t.net_pnl_dollars < 0))
    if gross_loss == 0:
        return float("inf") if gross_profit > 0 else 0.0
    return gross_profit / gross_loss


def format_report(
    result: BacktestResult,
    config: dict | None = None,
    predictions_path: str | None = None,
    data_path: str | None = None,
) -> str:
    """Format a BacktestResult as a structured console report.

    Args:
        result: BacktestResult from a backtest run.
        config: Optional strategy config dict for metadata header.
        predictions_path: Optional path shown in the header.
        data_path: Optional data path shown in the header.
    """
    w = 90
    lines: list[str] = []

    # ── Header: Model / Strategy Metadata ──
    lines.append("=" * w)
    title = f"BACKTEST REPORT: {result.label}" if result.label else "BACKTEST REPORT"
    lines.append(title.center(w))
    lines.append("=" * w)

    if config:
        nickname = config.get("nickname", "?")
        models = config.get("models", {})
        if models:
            long_id = models.get("long", {}).get("experiment_id", "N/A")
            short_id = models.get("short", {}).get("experiment_id", "N/A")
            # Derive effective thresholds from tiers (source of truth for
            # TieredEnsembleStrategy) with fallback to models.*.threshold
            # for non-tiered strategies.
            long_tiers = config.get("long", {}).get("tiers", [])
            short_tiers = config.get("short", {}).get("tiers", [])
            if long_tiers:
                long_thr = min(float(t.get("min_prob", 1.0)) for t in long_tiers)
            else:
                long_thr = models.get("long", {}).get("threshold", "?")
            if short_tiers:
                short_thr = min(float(t.get("min_prob", 1.0)) for t in short_tiers)
            else:
                short_thr = models.get("short", {}).get("threshold", "?")
            lines.append(f"  Strategy:       {nickname}")
            lines.append(f"  Models:         {long_id} (Long) + {short_id} (Short)")
            lines.append(f"  Threshold:      Buy >= {long_thr}, Sell >= {short_thr}")
        else:
            lines.append(f"  Strategy:       {nickname}")
            direction = config.get("direction", "?")
            threshold = config.get("entry_threshold", "?")
            lines.append(f"  Direction:      {direction}")
            lines.append(f"  Threshold:      {threshold}")

        # For dual-model ensemble configs, show per-side TP/SL/Trailing
        if models and "long" in config and "short" in config:
            tp_l = config["long"].get("tp_atr_mult", config.get("tp_atr_mult", "?"))
            sl_l = config["long"].get("sl_atr_mult", config.get("sl_atr_mult", "?"))
            tr_l = config["long"].get("trailing_atr_mult", config.get("trailing_atr_mult", "?"))
            tp_s = config["short"].get("tp_atr_mult", config.get("tp_atr_mult", "?"))
            sl_s = config["short"].get("sl_atr_mult", config.get("sl_atr_mult", "?"))
            tr_s = config["short"].get("trailing_atr_mult", config.get("trailing_atr_mult", "?"))
            lines.append(f"  TP / SL:        {tp_l}x / {sl_l}x ATR (Long), {tp_s}x / {sl_s}x ATR (Short)")
            tr_l_note = " (disabled)" if isinstance(tr_l, (int, float)) and tr_l >= 50 else ""
            tr_s_note = " (disabled)" if isinstance(tr_s, (int, float)) and tr_s >= 50 else ""
            lines.append(f"  Trailing:       {tr_l}x{tr_l_note} (Long), {tr_s}x{tr_s_note} (Short)")
        else:
            tp = config.get("tp_atr_mult") or config.get("long", {}).get("tp_atr_mult", "?")
            sl = config.get("sl_atr_mult") or config.get("long", {}).get("sl_atr_mult", "?")
            trailing = config.get("trailing_atr_mult") or config.get("long", {}).get("trailing_atr_mult", "?")
            lines.append(f"  TP / SL:        {tp}x / {sl}x ATR")
            trailing_note = " (disabled)" if isinstance(trailing, (int, float)) and trailing >= 50 else ""
            lines.append(f"  Trailing:       {trailing}x{trailing_note}")

    if predictions_path:
        lines.append(f"  Predictions:    {predictions_path}")
    if data_path:
        lines.append(f"  Data:           {data_path}")

    # Prediction date range (from actual trades)
    if result.trades:
        entries = [t.entry_dt for t in result.trades]
        lines.append(f"  Trade Range:    {min(entries)} -> {max(entries)}")

    lines.append("=" * w)

    if result.trade_count == 0:
        lines.append("  No trades simulated.")
        lines.append("=" * w)
        return "\n".join(lines)

    # ── Aggregate Summary ──
    lines.append("  AGGREGATE SUMMARY")
    lines.append("-" * w)
    lines.append(f"  Total Trades:     {result.trade_count}")
    lines.append(f"  Blocked Trades:   {result.blocked_trades_count}")
    lines.append(f"  Win Rate:         {result.win_rate:.1%}")
    lines.append(f"  Profit Factor:    {result.profit_factor:.2f}")
    lines.append(f"  Total Net PnL:    ${result.total_pnl:>14,.2f}")
    lines.append(f"  Max Drawdown:     ${result.max_drawdown:>14,.2f}")

    # ── Monthly Breakdown Table ──
    lines.append("")
    lines.append("=" * w)
    lines.append("  MONTHLY BREAKDOWN")
    lines.append("-" * w)
    hdr = (
        f"  {'Month':<10s} | {'Trades':>6s} | {'Buys':>5s} | {'Sells':>5s} |"
        f" {'WR%':>6s} | {'Buy WR':>6s} | {'Sell WR':>7s} |"
        f" {'Net PnL':>11s} | {'PF':>5s}"
    )
    lines.append(hdr)
    lines.append("-" * w)

    # Group trades by month
    from collections import defaultdict
    monthly: dict[str, list[TradeRecord]] = defaultdict(list)

    if result.start_dt and result.end_dt:
        start_month = result.start_dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        dt_range = pd.date_range(start=start_month, end=result.end_dt, freq='MS')
        for dt in dt_range:
            monthly[dt.strftime("%Y-%m")] = []
    elif result.trades:
        start_month = min(t.entry_dt for t in result.trades).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end_month = max(t.entry_dt for t in result.trades)
        dt_range = pd.date_range(start=start_month, end=end_month, freq='MS')
        for dt in dt_range:
            monthly[dt.strftime("%Y-%m")] = []

    for t in result.trades:
        key = t.entry_dt.strftime("%Y-%m")
        monthly[key].append(t)

    current_year = None
    year_trades: list[TradeRecord] = []

    for month_key in sorted(monthly.keys()):
        year = month_key[:4]

        # Yearly subtotal separator
        if current_year is not None and year != current_year and year_trades:
            _append_subtotal(lines, f"{current_year} Total", year_trades, w, bold=True)
            year_trades = []

        current_year = year
        trades = monthly[month_key]
        year_trades.extend(trades)

        _append_month_row(lines, month_key, trades)

    # Final year subtotal
    if current_year is not None and year_trades:
        _append_subtotal(lines, f"{current_year} Total", year_trades, w, bold=True)

    # Grand total
    lines.append("=" * w)
    _append_subtotal(lines, "GRAND TOTAL", result.trades, w, bold=False)
    lines.append("=" * w)

    # ── Exit Distribution by Side ──
    lines.append("")
    lines.append("  EXIT DISTRIBUTION BY SIDE")
    lines.append("-" * w)
    lines.append(
        f"  {'Exit Reason':<16s} | {'Long':>7s} | {'Short':>7s} | {'Total':>7s}"
    )
    lines.append("-" * w)

    long_trades = [t for t in result.trades if t.side == 1]
    short_trades = [t for t in result.trades if t.side == -1]

    for reason in ["TP", "SL", "TRAILING_BE", "TIME_BARRIER", "SIGNAL_EXIT", "WEEKEND_FLATTEN", "EOD_FLATTEN"]:
        long_count = sum(
            1 for t in long_trades if t.exit_reason.value == reason
        )
        short_count = sum(
            1 for t in short_trades if t.exit_reason.value == reason
        )
        total_count = long_count + short_count
        lines.append(
            f"  {reason:<16s} | {long_count:>7d} | {short_count:>7d} | {total_count:>7d}"
        )

    lines.append("-" * w)

    # ── Notable Periods ──
    lines.append("")
    lines.append("  NOTABLE PERIODS")
    lines.append("-" * w)

    if monthly:
        # Best / worst month by PnL
        month_pnl = {
            k: sum(t.net_pnl_dollars for t in v) for k, v in monthly.items()
        }
        best_month = max(month_pnl, key=month_pnl.get)  # type: ignore[arg-type]
        worst_month = min(month_pnl, key=month_pnl.get)  # type: ignore[arg-type]
        best_wr = sum(1 for t in monthly[best_month] if t.net_pnl_dollars > 0) / max(len(monthly[best_month]), 1)
        worst_wr = sum(1 for t in monthly[worst_month] if t.net_pnl_dollars > 0) / max(len(monthly[worst_month]), 1)

        lines.append(
            f"  Best Month:       {best_month}  "
            f"(${month_pnl[best_month]:>10,.2f}, {best_wr:.1%} WR, "
            f"{len(monthly[best_month])} trades)"
        )
        lines.append(
            f"  Worst Month:      {worst_month}  "
            f"(${month_pnl[worst_month]:>10,.2f}, {worst_wr:.1%} WR, "
            f"{len(monthly[worst_month])} trades)"
        )

    # Win / loss streaks
    if result.trades:
        max_win_streak = 0
        max_loss_streak = 0
        cur_win = 0
        cur_loss = 0
        for t in result.trades:
            if t.net_pnl_dollars > 0:
                cur_win += 1
                cur_loss = 0
            else:
                cur_loss += 1
                cur_win = 0
            max_win_streak = max(max_win_streak, cur_win)
            max_loss_streak = max(max_loss_streak, cur_loss)

        lines.append(f"  Win Streak:       {max_win_streak} trades")
        lines.append(f"  Loss Streak:      {max_loss_streak} trades")

        best_trade = max(result.trades, key=lambda t: t.net_pnl_dollars)
        worst_trade = min(result.trades, key=lambda t: t.net_pnl_dollars)
        lines.append(
            f"  Best Trade:       ${best_trade.net_pnl_dollars:>10,.2f}  "
            f"({best_trade.entry_dt.strftime('%Y-%m-%d %H:%M')}, "
            f"{'Long' if best_trade.side == 1 else 'Short'})"
        )
        lines.append(
            f"  Worst Trade:      ${worst_trade.net_pnl_dollars:>10,.2f}  "
            f"({worst_trade.entry_dt.strftime('%Y-%m-%d %H:%M')}, "
            f"{'Long' if worst_trade.side == 1 else 'Short'})"
        )

    lines.append("=" * w)
    return "\n".join(lines)


def _append_month_row(
    lines: list[str],
    label: str,
    trades: list[TradeRecord],
) -> None:
    """Append one month row to the report lines."""
    total = len(trades)
    buys = [t for t in trades if t.side == 1]
    sells = [t for t in trades if t.side == -1]
    wins = sum(1 for t in trades if t.net_pnl_dollars > 0)
    buy_wins = sum(1 for t in buys if t.net_pnl_dollars > 0)
    sell_wins = sum(1 for t in sells if t.net_pnl_dollars > 0)
    pnl = sum(t.net_pnl_dollars for t in trades)
    pf = _safe_pf(trades)
    pf_str = f"{pf:>5.2f}" if pf < 100 else "  inf"

    lines.append(
        f"  {label:<10s} | {total:>6d} | {len(buys):>5d} | {len(sells):>5d} |"
        f" {_safe_wr(wins, total)} | {_safe_wr(buy_wins, len(buys))} |"
        f" {_safe_wr(sell_wins, len(sells))}  |"
        f" ${pnl:>10,.2f} | {pf_str}"
    )


def _append_subtotal(
    lines: list[str],
    label: str,
    trades: list[TradeRecord],
    w: int,
    bold: bool = False,
) -> None:
    """Append a subtotal row (year or grand total)."""
    if bold:
        lines.append("-" * w)
    _append_month_row(lines, label, trades)
    if bold:
        lines.append("=" * w)


def compare_runs(
    result_a: BacktestResult,
    result_b: BacktestResult,
) -> str:
    """Format a side-by-side A/B comparison of two backtest runs."""
    w = 70
    lines: list[str] = []
    lines.append("=" * w)
    lines.append("A/B COMPARISON".center(w))
    lines.append("=" * w)

    label_a = result_a.label or "Run A"
    label_b = result_b.label or "Run B"

    header = f"  {'Metric':<24s} {label_a:>18s} {label_b:>18s}"
    lines.append(header)
    lines.append("-" * w)

    rows = [
        ("Trade Count", f"{result_a.trade_count}", f"{result_b.trade_count}"),
        ("Win Rate", f"{result_a.win_rate:.1%}", f"{result_b.win_rate:.1%}"),
        (
            "Profit Factor",
            f"{result_a.profit_factor:.2f}",
            f"{result_b.profit_factor:.2f}",
        ),
        (
            "Total Net PnL",
            f"${result_a.total_pnl:,.2f}",
            f"${result_b.total_pnl:,.2f}",
        ),
        (
            "Max Drawdown",
            f"${result_a.max_drawdown:,.2f}",
            f"${result_b.max_drawdown:,.2f}",
        ),
    ]

    for label, val_a, val_b in rows:
        lines.append(f"  {label:<24s} {val_a:>18s} {val_b:>18s}")

    lines.append("-" * w)
    lines.append("  Exit Distribution:")

    dist_a = result_a.exit_distribution
    dist_b = result_b.exit_distribution
    for reason in ["TP", "SL", "TRAILING_BE", "TIME_BARRIER", "SIGNAL_EXIT", "WEEKEND_FLATTEN", "EOD_FLATTEN"]:
        da = dist_a.get(reason, {"count": 0, "pct": 0.0})
        db = dist_b.get(reason, {"count": 0, "pct": 0.0})
        val_a_str = f"{int(da['count'])} ({da['pct']:.1f}%)"
        val_b_str = f"{int(db['count'])} ({db['pct']:.1f}%)"
        lines.append(f"    {reason:<16s} {val_a_str:>18s} {val_b_str:>18s}")

    lines.append("=" * w)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Data Loading Utilities
# ---------------------------------------------------------------------------


def _resolve_prob_column(df: pd.DataFrame, keyword: str) -> str | None:
    """Find a column containing `keyword` (case-insensitive).

    Searches for columns matching common patterns like 'prob_buy',
    'prob_Buy', 'PROB_BUY', 'probability_buy', etc.

    Returns the original column name if found, else None.
    """
    keyword_lower = keyword.lower()
    for col in df.columns:
        if keyword_lower in col.lower():
            return col
    return None


def load_predictions(path: str) -> pd.DataFrame:
    """Load vault predictions CSV, returning a DataFrame with DatetimeIndex.

    Handles both new-format (prob_Buy column) and legacy-format (Predicted 0/1).
    Gracefully skips malformed lines.
    """
    df = pd.read_csv(
        path, index_col=0, parse_dates=True, on_bad_lines="warn"
    )
    return df


def load_ohlcv(path: str) -> pd.DataFrame:
    """Load OHLCV data from parquet or CSV.

    Returns a DataFrame with columns: Open, High, Low, Close, Volume
    indexed by DateTime.
    """
    if path.endswith(".parquet"):
        df = pd.read_parquet(path)
    else:
        df = pd.read_csv(path, index_col=0, parse_dates=True, sep=None, engine="python")

    if "DateTime" in df.columns:
        df["DateTime"] = pd.to_datetime(df["DateTime"])
        df = df.set_index("DateTime")

    # Ensure standard column names
    rename_map = {
        "open": "Open",
        "high": "High",
        "low": "Low",
        "close": "Close",
        "volume": "Volume",
    }
    df = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns})

    required = {"Open", "High", "Low", "Close", "Volume"}
    missing = required - set(df.columns)
    if missing:
        # Try parsing as headerless semicolon-separated CSV (Databento pipeline format)
        try:
            df = pd.read_csv(
                path,
                sep=";",
                header=None,
                names=["Date", "Time", "Open", "High", "Low", "Close", "Volume"],
            )
            df["DateTime"] = pd.to_datetime(
                df["Date"] + " " + df["Time"], dayfirst=True
            )
            df = df.set_index("DateTime")
            df = df.drop(columns=["Date", "Time"])
            for col in ["Open", "High", "Low", "Close", "Volume"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            print(f"  Parsed as headerless semicolon CSV: {len(df):,} rows")
        except Exception:
            raise FileNotFoundError(
                f"OHLCV data at {path} is missing columns {missing} "
                f"and could not be parsed as a headerless semicolon CSV."
            )

    return df


def load_ohlcv_dual(path: str) -> tuple[pd.DataFrame, pd.DataFrame | None]:
    """Load OHLCV, extracting separate execution DataFrame if EXEC_ columns exist.

    Returns:
        (ohlcv_features, ohlcv_exec):
            ohlcv_features — ratio-adjusted OHLCV (for brain/features/ATR)
            ohlcv_exec — raw unadjusted OHLCV (for wallet/fills/PnL), or None
    """
    df = load_ohlcv(path)  # existing function, handles RAW_ overwrite etc.

    if "EXEC_Close" in df.columns:
        exec_cols = {"Open": "EXEC_Open", "High": "EXEC_High",
                     "Low": "EXEC_Low", "Close": "EXEC_Close"}
        ohlcv_exec = pd.DataFrame(index=df.index)
        for std_col, exec_col in exec_cols.items():
            if exec_col in df.columns:
                ohlcv_exec[std_col] = df[exec_col]
            else:
                ohlcv_exec[std_col] = df[std_col]  # fallback
        if "Volume" in df.columns:
            ohlcv_exec["Volume"] = df["Volume"]
        return df, ohlcv_exec
    else:
        return df, None


# ---------------------------------------------------------------------------
# CLI Entry Point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="BacktestEngine — Config-Driven Strategy Backtester"
    )
    parser.add_argument(
        "--retrain-every",
        default=None,
        help="Pandas offset string (e.g. 3M, 6M) for Walk-Forward periodic retraining.",
    )
    parser.add_argument(
        "--oos-start",
        default=None,
        help="Start date for Out-Of-Sample Walk-Forward timeline (e.g., 2024-01-01). Required if --retrain-every is set.",
    )
    parser.add_argument(
        "--predictions",
        default=None,
        help="Path to predictions CSV. If omitted and --config is given, "
             "auto-resolves from config's models.*.predictions_path",
    )
    parser.add_argument(
        "--data",
        default="data/processed/CL_set_06.parquet",
        help="Path to OHLCV parquet for Run A (historical)",
    )
    parser.add_argument(
        "--exec-data",
        default=None,
        help="Optional path to raw unadjusted execution data for wallet/fills",
    )
    parser.add_argument(
        "--live-data",
        default=None,
        help="Optional path to live session OHLCV parquet for Run B",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to a strategy JSON config (reads all parameters)",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.45,
        help="Probability threshold (default: 0.45)",
    )
    parser.add_argument(
        "--tp-mult", type=float, default=2.0, help="TP ATR multiplier"
    )
    parser.add_argument(
        "--sl-mult", type=float, default=1.0, help="SL ATR multiplier"
    )
    parser.add_argument(
        "--commission-per-side", type=float, default=2.50, help="Commission per side ($)"
    )
    parser.add_argument(
        "--slippage-per-side", type=float, default=None,
        help="Slippage per side in price points. Default: the config symbol's "
             "1-tick value from the instrument registry (CL resolves to 0.01, "
             "identical to the old hardcoded default); legacy 0.01 when no "
             "config/symbol."
    )
    parser.add_argument(
        "--contract-multiplier", type=float, default=None,
        help="Dollar value per 1.0 price move. Default: resolved from the "
             "config's execution_symbol via the instrument registry "
             "(CL resolves to 1000.0); legacy 1000.0 when no config/symbol."
    )
    parser.add_argument(
        "--report-file", default=None,
        help="Optional path to save the full report as a text file",
    )
    parser.add_argument(
        "--holdout-report", action="store_true", default=False,
        help="Run a secondary backtest on the holdout period (last N months) "
             "and print both reports.  N is read from config holdout_months "
             "or --holdout-months.",
    )
    parser.add_argument(
        "--holdout-months", type=int, default=None,
        help="Override holdout_months from config (used with --holdout-report)",
    )
    args = parser.parse_args()

    # Resolve paths via CL_DATA_ROOT fallback
    from src.data_paths import resolve_cli_path
    if args.predictions:
        args.predictions = resolve_cli_path(args.predictions)
    args.data = resolve_cli_path(args.data)
    if args.live_data:
        args.live_data = resolve_cli_path(args.live_data)
    if args.config:
        args.config = resolve_cli_path(args.config)

    # If --config is provided, create engine from strategy JSON
    if args.config is not None:
        from src.live_execution.config_loader import load_strategy_config
        strategy_cfg = load_strategy_config(args.config)

        # Resolve economics: explicit CLI flags win; otherwise the config's
        # execution_symbol via the instrument registry (CL -> 1000.0 / 0.01,
        # byte-identical to the legacy defaults); legacy values + warning when
        # the config predates symbol stamping.
        if args.contract_multiplier is None or args.slippage_per_side is None:
            _cfg_symbol = strategy_cfg.get("execution_symbol")
            if _cfg_symbol:
                from src.core.instrument_master import (
                    default_slippage_points,
                    dollars_per_point,
                )
                if args.contract_multiplier is None:
                    args.contract_multiplier = dollars_per_point(_cfg_symbol)
                    print(f"Contract multiplier: {args.contract_multiplier} $/pt "
                          f"(resolved from execution_symbol={_cfg_symbol})")
                if args.slippage_per_side is None:
                    args.slippage_per_side = default_slippage_points(_cfg_symbol)
                    print(f"Slippage per side: {args.slippage_per_side} pts "
                          f"(1 tick, resolved from execution_symbol={_cfg_symbol})")
            else:
                if args.contract_multiplier is None:
                    args.contract_multiplier = 1000.0
                if args.slippage_per_side is None:
                    args.slippage_per_side = 0.01
                print("WARNING: no execution_symbol in config — falling back to "
                      "legacy CL economics (1000.0 $/pt, 0.01 slippage/side).")

        bt = BacktestEngine.from_config(
            strategy_cfg,
            commission_per_side=args.commission_per_side,
            slippage_per_side=args.slippage_per_side,
            contract_multiplier=args.contract_multiplier,
        )
        concurrent_str = (
            f"concurrent={bt.max_concurrent}"
            if bt.allow_concurrent
            else "single-position"
        )
        # Display the correct threshold(s) depending on which path is used
        strat = bt._execution_strategy
        if strat is not None and hasattr(strat, "long_threshold"):
            threshold_str = (
                f"threshold(buy={strat.long_threshold}, sell={strat.short_threshold})"
            )
        elif strat is not None and hasattr(strat, "threshold"):
            threshold_str = f"threshold={strat.threshold}"
        else:
            threshold_str = f"threshold={bt.prob_threshold}"
        print(
            f"Loaded strategy config '{strategy_cfg.get('nickname', '?')}': "
            f"TP={bt.tp_atr_mult}x  SL={bt.sl_atr_mult}x  "
            f"{threshold_str}  [{concurrent_str}]"
        )
        strategy_cfg = strategy_cfg  # already loaded
    else:
        strategy_cfg = None  # no config provided
        if args.contract_multiplier is None:
            args.contract_multiplier = 1000.0  # legacy CL defaults (no config to resolve from)
        if args.slippage_per_side is None:
            args.slippage_per_side = 0.01
        bt = BacktestEngine(
            tp_atr_mult=args.tp_mult,
            sl_atr_mult=args.sl_mult,
            prob_threshold=args.threshold,
            commission_per_side=args.commission_per_side,
            slippage_per_side=args.slippage_per_side,
            contract_multiplier=args.contract_multiplier,
        )

    # Load historical data
    print(f"Loading historical OHLCV from {args.data}...")
    ohlcv_a, ohlcv_exec_a = load_ohlcv_dual(args.data)
    if args.exec_data:
        ohlcv_exec_a = load_ohlcv(args.exec_data)

        if len(ohlcv_a) > 1 and len(ohlcv_exec_a) > 1:
            diff_main = pd.Series(ohlcv_a.index).diff().median()
            diff_exec = pd.Series(ohlcv_exec_a.index).diff().median()
            if diff_exec < diff_main:
                raise ValueError(
                    f"FATAL MISMATCH: Execution dataset resolution ({diff_exec}) is higher "
                    f"than inference dataset resolution ({diff_main}). This will cause the "
                    f"engine's .reindex() function to slice a single intra-bar tick instead "
                    f"of aggregating the full bar's High/Low range. Pass a properly aggregated "
                    f"unadjusted dataset instead."
                )

    if args.retrain_every is not None:
        if not args.oos_start:
            raise ValueError("--oos-start must be provided if --retrain-every is used (e.g., --oos-start 2024-01-01).")
        
        print(f"\n--- INIT WALK-FORWARD PERIODIC RETRAINING: {args.retrain_every} ---")
        oos_start = pd.Timestamp(args.oos_start)
        
        if not args.data.endswith(".parquet"):
            raise ValueError("Walk-Forward Retraining requires the full .parquet feature dataset.")
            
        models_cfg = strategy_cfg.get("models", {}) if strategy_cfg else {}
        
        best_params = {}
        for side in ["long", "short"]:
            cfg_side = models_cfg.get(side, {})
            exp_id = cfg_side.get("experiment_id")
            if exp_id:
                bp_path = os.path.join(PROJECT_ROOT, "models", "registry", exp_id, "best_params.json")
                try:
                    with open(bp_path, "r") as f:
                        best_params[side] = json.load(f)
                    print(f"Loaded {side} params: {bp_path}")
                except FileNotFoundError:
                    print(f"WARNING: No best_params.json found for {side} at {bp_path}")
                    best_params[side] = {}
            else:
                best_params[side] = {}
                
        chunks = list(pd.date_range(start=oos_start, end=ohlcv_a.index.max(), freq=args.retrain_every))
        if len(chunks) == 0 or chunks[-1] < ohlcv_a.index.max():
            chunks.append(ohlcv_a.index.max())
            
        # --- Compute purge gap from target horizon ---
        # Triple-barrier targets encode the horizon in their name, e.g.
        # TARGET_TRIPLE_2x1_24H_LONG → 24 hours.  We parse it to create
        # a purge gap between the training end and OOS start, preventing
        # the last N training labels from peeking into OOS feature data.
        _purge_hours = 48  # conservative default (matches 576 bars at 5-min)
        import re as _re
        for _side_key in ("long", "short"):
            _tgt = models_cfg.get(_side_key, {}).get("target", "")
            _m = _re.search(r"_(\d+)H(?:_|$)", _tgt)
            if _m:
                _purge_hours = max(_purge_hours, int(_m.group(1)))
        _purge_offset = pd.Timedelta(hours=_purge_hours)
        print(f"  Purge gap: {_purge_hours}H ({_purge_offset}) — prevents target label leakage at train/OOS boundary")

        oos_preds = []
        for i in range(len(chunks) - 1):
            chunk_start = chunks[i]
            chunk_end = chunks[i+1]
            print(f"\n[WF] Chunk {i+1}: {chunk_start.strftime('%Y-%m-%d')} to {chunk_end.strftime('%Y-%m-%d')}")
            
            lb_years_long = models_cfg.get("long", {}).get("lookback_window_years")
            lb_years_short = models_cfg.get("short", {}).get("lookback_window_years")
            
            is_start_long = chunk_start - DateOffset(years=lb_years_long) if lb_years_long else ohlcv_a.index.min()
            is_start_short = chunk_start - DateOffset(years=lb_years_short) if lb_years_short else ohlcv_a.index.min()
            
            # Apply purge gap: training data must end purge_hours before
            # the OOS window starts so that target labels (which look forward
            # by up to the target horizon) cannot peek into OOS data.
            train_end_long = chunk_start - _purge_offset
            train_end_short = chunk_start - _purge_offset
            
            is_df_long = ohlcv_a[(ohlcv_a.index >= is_start_long) & (ohlcv_a.index < train_end_long)]
            is_df_short = ohlcv_a[(ohlcv_a.index >= is_start_short) & (ohlcv_a.index < train_end_short)]
            
            oos_df = ohlcv_a[(ohlcv_a.index >= chunk_start) & (ohlcv_a.index < chunk_end)]
            if oos_df.empty:
                continue
                
            chunk_preds = pd.DataFrame(index=oos_df.index)
            
            if "long" in models_cfg:
                target_name = models_cfg["long"].get("target", "TARGET_DIR_8PCT_MULTI")
                X_train_l, y_train_l = get_X_y(is_df_long, target_name=target_name)
                valid_mask = ~y_train_l.isna()
                X_train_l, y_train_l = X_train_l[valid_mask], y_train_l[valid_mask]
                
                print(f"  Training Long model: {len(X_train_l):,} rows (IS start: {is_start_long.strftime('%Y-%m-%d')})")
                model_long = LGBMLearner(**best_params["long"])
                model_long.add_evidence(X_train_l, y_train_l)
                
                X_test_l, _ = get_X_y(oos_df, target_name=target_name)
                preds_l = model_long.model.predict(X_test_l)
                if preds_l.ndim == 2 and preds_l.shape[1] >= 2:
                    prob_buy = preds_l[:, 1]
                else:
                    prob_buy = preds_l
                chunk_preds["prob_Buy"] = prob_buy
            
            if "short" in models_cfg:
                target_name = models_cfg["short"].get("target", "TARGET_DIR_8PCT_MULTI")
                X_train_s, y_train_s = get_X_y(is_df_short, target_name=target_name)
                valid_mask = ~y_train_s.isna()
                X_train_s, y_train_s = X_train_s[valid_mask], y_train_s[valid_mask]
                
                print(f"  Training Short model: {len(X_train_s):,} rows (IS start: {is_start_short.strftime('%Y-%m-%d')})")
                model_short = LGBMLearner(**best_params["short"])
                model_short.add_evidence(X_train_s, y_train_s)
                
                X_test_s, _ = get_X_y(oos_df, target_name=target_name)
                preds_s = model_short.model.predict(X_test_s)
                if preds_s.ndim == 2 and preds_s.shape[1] >= 3:
                    prob_sell = preds_s[:, 2]
                elif preds_s.ndim == 2 and preds_s.shape[1] == 2:
                    prob_sell = preds_s[:, 1]
                else:
                    prob_sell = preds_s
                chunk_preds["prob_Sell"] = prob_sell

            oos_preds.append(chunk_preds)
            
        if len(oos_preds) > 0:
            preds = pd.concat(oos_preds).fillna(0.0)
            print(f"\n[WF] Stitching Complete. Generated {len(preds):,} OOS predictions.")
        else:
            print("\n[WF] WARNING: Generated 0 chunks. Fallback to empty predictions.")
            preds = pd.DataFrame()

    else:
        # Auto-resolve predictions from config if not explicitly provided
        if args.predictions is None and strategy_cfg is not None:
            models_cfg = strategy_cfg.get("models", {})
            long_preds_path = models_cfg.get("long", {}).get("predictions_path")
            short_preds_path = models_cfg.get("short", {}).get("predictions_path")

            def _resolve_config_path(path_str):
                if not path_str: return path_str
                if path_str.startswith(".") or path_str.startswith(".."):
                    if args.config:
                        return os.path.normpath(os.path.join(os.path.dirname(args.config), path_str))
                return resolve_cli_path(path_str)
    
            if long_preds_path and short_preds_path:
                # Dual-model: auto-merge long + short predictions
                long_preds_path = _resolve_config_path(long_preds_path)
                short_preds_path = _resolve_config_path(short_preds_path)
                print(f"Auto-resolving predictions from config (dual-model):")
                print(f"  Long:  {long_preds_path}")
                print(f"  Short: {short_preds_path}")
                long_df = load_predictions(long_preds_path)
                short_df = load_predictions(short_preds_path)
                # Find probability columns (case-insensitive, strict validation)
                long_col = _resolve_prob_column(long_df, "buy")
                short_col = _resolve_prob_column(short_df, "sell")
                if long_col is None:
                    raise ValueError(
                        f"Long model predictions ({long_preds_path}) have no column "
                        f"containing 'buy' (found: {list(long_df.columns)}). "
                        f"Cannot silently use another column as prob_buy."
                    )
                if short_col is None:
                    raise ValueError(
                        f"Short model predictions ({short_preds_path}) have no column "
                        f"containing 'sell' (found: {list(short_df.columns)}). "
                        f"Cannot silently use another column as prob_sell."
                    )
                long_probs = long_df[[long_col]].rename(columns={long_col: "prob_Buy"})
                short_probs = short_df[[short_col]].rename(columns={short_col: "prob_Sell"})
                preds = long_probs.join(short_probs, how="outer").fillna(0.0)
                print(f"  Merged: {len(preds):,} rows ({preds['prob_Buy'].gt(0).sum():,} buy signals, {preds['prob_Sell'].gt(0).sum():,} sell signals)")
            elif long_preds_path:
                long_preds_path = _resolve_config_path(long_preds_path)
                print(f"Auto-resolving predictions from config: {long_preds_path}")
                preds = load_predictions(long_preds_path)
            elif short_preds_path:
                short_preds_path = _resolve_config_path(short_preds_path)
                print(f"Auto-resolving predictions from config: {short_preds_path}")
                preds = load_predictions(short_preds_path)
            else:
                print("WARNING: No predictions_path in config and no --predictions flag. Using default.")
                args.predictions = resolve_cli_path("reports/vault_predictions.csv")
                preds = load_predictions(args.predictions)
        elif args.predictions is None:
            # No config and no predictions — use legacy default
            args.predictions = resolve_cli_path("reports/vault_predictions.csv")
            print(f"Loading predictions from {args.predictions}...")
            preds = load_predictions(args.predictions)
        else:
            print(f"Loading predictions from {args.predictions}...")
            preds = load_predictions(args.predictions)

    print("Running backtest on historical data...")
    result_a = bt.run(preds, ohlcv_a, ohlcv_exec_df=ohlcv_exec_a, label="Historical")
    strategy_cfg_for_report = strategy_cfg if args.config else None
    report_text = format_report(
        result_a,
        config=strategy_cfg_for_report,
        predictions_path=args.predictions or "(auto-resolved from config)",
        data_path=args.data,
    )
    print()
    print(report_text)

    if args.report_file:
        os.makedirs(os.path.dirname(args.report_file) or ".", exist_ok=True)
        with open(args.report_file, "w", encoding="utf-8") as rf:
            rf.write(report_text + "\n")
        print(f"\nReport saved to {args.report_file}")

    # Auto-save report to model registry if experiment_id is available from config
    if strategy_cfg is not None:
        models_cfg = strategy_cfg.get("models", {})
        exp_id = (
            models_cfg.get("long", {}).get("experiment_id")
            or models_cfg.get("short", {}).get("experiment_id")
        )
        if exp_id:
            registry_dir = os.path.join("models", "registry", exp_id)
            if os.path.isdir(registry_dir):
                registry_report = os.path.join(registry_dir, "backtest_report.txt")
                with open(registry_report, "w", encoding="utf-8") as rf:
                    rf.write(report_text + "\n")
                print(f"Report auto-saved to registry: {registry_report}")

    # ── Holdout report (secondary backtest on unseen period) ──────────
    holdout_months = args.holdout_months
    if holdout_months is None and strategy_cfg is not None:
        holdout_months = strategy_cfg.get("holdout_months", 0)
    if holdout_months is None:
        holdout_months = 0

    if holdout_months > 0:
        pred_end = preds.index.max()
        holdout_cutoff = pred_end - pd.DateOffset(months=holdout_months)
        holdout_preds = preds[preds.index >= holdout_cutoff]
        optimizer_preds = preds[preds.index < holdout_cutoff]

        if len(holdout_preds) == 0:
            print(f"\nWARNING: Holdout period ({holdout_months} months) has 0 prediction rows.")
        else:
            print(f"\n{'='*90}")
            print(f"HOLDOUT REPORT  ({holdout_months} months: "
                  f"{holdout_cutoff.date()} -> {pred_end.date()}, "
                  f"{len(holdout_preds):,} bars)")
            print(f"{'='*90}")

            # Re-run on optimizer window only
            bt_opt = BacktestEngine.from_config(
                strategy_cfg,
                commission_per_side=args.commission_per_side,
                slippage_per_side=args.slippage_per_side,
                contract_multiplier=args.contract_multiplier,
            ) if strategy_cfg else bt
            result_opt = bt_opt.run(optimizer_preds, ohlcv_a, ohlcv_exec_df=ohlcv_exec_a, label="Optimizer Window")

            # Run on holdout only
            bt_ho = BacktestEngine.from_config(
                strategy_cfg,
                commission_per_side=args.commission_per_side,
                slippage_per_side=args.slippage_per_side,
                contract_multiplier=args.contract_multiplier,
            ) if strategy_cfg else bt
            result_ho = bt_ho.run(holdout_preds, ohlcv_a, ohlcv_exec_df=ohlcv_exec_a, label="Holdout (Unseen)")

            ho_report = format_report(
                result_ho,
                config=strategy_cfg_for_report,
                predictions_path=f"holdout: {holdout_cutoff.date()} -> {pred_end.date()}",
                data_path=args.data,
            )
            print(ho_report)

            # Side-by-side comparison
            print()
            print(compare_runs(result_opt, result_ho))

    # Run B: Live session data (optional)
    if args.live_data and os.path.exists(args.live_data):
        print(f"\nLoading live session OHLCV from {args.live_data}...")
        ohlcv_b = load_ohlcv(args.live_data)

        print("Running backtest on live session data...")
        result_b = bt.run(preds, ohlcv_b, label="Live Session")
        print()
        print(format_report(result_b))

        # A/B Comparison
        print()
        print(compare_runs(result_a, result_b))
    elif args.live_data:
        print(f"\nNote: Live data file not found at {args.live_data} — skipping Run B.")


# Backward-compatible alias
CLAdvancedExecutionBacktester = BacktestEngine


if __name__ == "__main__":
    main()
