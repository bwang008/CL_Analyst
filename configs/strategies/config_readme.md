# Strategy Config Reference

All strategy configs live in `configs/strategies/` as JSON files.  
This document explains every attribute, its default value, and which systems read it.

---

## Config Structure

Configs are organized into two sections:
- **Top-level**: Shared fields used by both BacktestEngine and LiveTrader
- **`live_config`**: Fields only used by the LiveTrader (ignored by BacktestEngine)

---

## Compatibility Matrix

| Attribute | BacktestEngine | LiveTrader | Old backtester.py |
|-----------|:-:|:-:|:-:|
| `nickname` | ✅ Display | ✅ Display | ❌ |
| `direction` | ✅ Strategy | ✅ Model dir | ❌ |
| `execution_class` | ✅ Factory | ❌ N/A | ❌ |
| `models` | ✅ Ensemble | ❌ N/A | ❌ |
| `entry_threshold` | ✅ | ✅ | ✅ |
| `tp_atr_mult` | ✅ | ✅ | ✅ |
| `sl_atr_mult` | ✅ | ✅ | ✅ |
| `trailing_atr_mult` | ✅ | ❌ | ❌ |
| `cooldown_bars` | ✅ | ❌ | ❌ |
| `max_hold_bars` | ✅ | ✅ | ✅ |
| `allow_concurrent` | ✅ | ❌ | ❌ |
| `max_concurrent` | ✅ | ❌ | ❌ |
| `sizing_tiers` | ✅ | ✅ | ✅ |
| `live_config.experiment_id` | ❌ | ✅ Model load | ❌ |
| `live_config.client_id` | ❌ | ✅ IB Gateway | ❌ |
| `live_config.entry_mode` | ❌ | ✅ Order type | ❌ |
| `live_config.exit_mode` | ❌ | ✅ Exit order type | ❌ |
| `live_config.adaptive_priority` | ❌ | ✅ Algo urgency | ❌ |

---

## Shared Attributes

### Identity

| Attribute | Type | Default | Description |
|-----------|------|---------|-------------|
| `nickname` | string | `"unnamed"` | Human-readable name for logging and reports |

### Execution Strategy

| Attribute | Type | Default | Description |
|-----------|------|---------|-------------|
| `execution_class` | string | `"SingleModelStrategy"` | Strategy class: `SingleModelStrategy`, `ConservativeEnsembleStrategy`, `AggressiveEnsembleStrategy`, `TieredEnsembleStrategy` |
| `direction` | string | `"LONG"` | `"LONG"` uses `prob_Buy`, `"SHORT"` uses `prob_Sell` |
| `models` | object | — | Ensemble only: per-direction thresholds, e.g. `{"long": {"threshold": 0.70}, "short": {"threshold": 0.60}}` |

### Signal Thresholds

| Attribute | Type | Default | Description |
|-----------|------|---------|-------------|
| `entry_threshold` | float | `0.45` | Min probability to trigger a trade |

### Trade Management

| Attribute | Type | Default | Description |
|-----------|------|---------|-------------|
| `tp_atr_mult` | float | `2.0` | Take-profit distance (× ATR) |
| `sl_atr_mult` | float | `1.0` | Stop-loss distance (× ATR) |
| `trailing_atr_mult` | float | `1.0` | ATR move in favor to shift SL to breakeven. ⚠ `0.0` triggers immediately (bug) |
| `cooldown_bars` | int | `10` | Bars to wait after SL before re-entering |
| `tp_cooldown_bars` | int | `cooldown_bars` | Bars to wait after TP/trailing exit (overrides cooldown_bars for TP) |
| `sl_cooldown_bars` | int | `cooldown_bars` | Bars to wait after SL exit (overrides cooldown_bars for SL) |
| `max_hold_bars` | int | `288` | Time barrier (288 = 24h on 5-min bars) |

### Position Management

| Attribute | Type | Default | Description |
|-----------|------|---------|-------------|
| `allow_concurrent` | bool | `false` | Allow multiple simultaneous positions |
| `max_concurrent` | int | `1` | Max open positions (when concurrent=true) |

### Position Sizing

| Attribute | Type | Default | Description |
|-----------|------|---------|-------------|
| `sizing_tiers` | object | `{}` | Maps min probability → lot count. Example: `{"0.80": 3, "0.70": 2, "0.60": 1}`. Highest-first matching. Falls back to 1 lot if no tier matches. |

---

## Live Config (`live_config` section)

These fields are **only used by the LiveTrader** and are ignored during backtesting.

| Attribute | Type | Default | Description |
|-----------|------|---------|-------------|
| `experiment_id` | string | — | Model registry folder name (e.g. `"EXP-017_S_Ultimate"`). Used to locate `models/registry/{id}/final_model.pkl` |
| `client_id` | int | `1` | IB Gateway client ID. Each strategy running concurrently needs a unique value to avoid connection conflicts |
| `entry_mode` | string | `"adaptive"` | Parent order type for entries: `"adaptive"` (IBKR algo seeks spread improvement), `"marketable_limit"` (limit 2 ticks through NBBO), or `"market"` (bare MKT). Also settable via CLI `--entry-mode`. |
| `exit_mode` | string | `"market"` | Order type for time-barrier exits: `"market"` (plain MKT), `"marketable_limit"` (limit 2 ticks through current price), or `"adaptive"` (IBKR algo, Urgent priority). |
| `adaptive_priority` | string | `"Normal"` | Urgency for Adaptive Algo: `"Normal"`, `"Urgent"`, or `"Patient"`. Only used when `entry_mode` is `"adaptive"`. Also settable via CLI `--adaptive-priority`. |

> [!NOTE]
> For backward compatibility, `experiment_id` is also read from the top-level config if `live_config.experiment_id` is not present.

> [!NOTE]
> CLI flags `--entry-mode` and `--adaptive-priority` take priority over config values.

---

## TieredEnsembleStrategy

The `TieredEnsembleStrategy` enables **asymmetric buy/sell tiers** with per-tier execution parameters. Each tier specifies its own TP, SL, trailing, max_hold, and lot count — these are passed to the engine as **per-Order overrides** (the engine uses per-trade values instead of globals when set).

### Config Schema

| Attribute | Type | Required | Description |
|-----------|------|:--------:|-------------|
| `execution_class` | `"TieredEnsembleStrategy"` | ✅ | Strategy class name |
| `long` | object | ✅ | Long-side configuration |
| `long.experiment_id` | string | — | Model registry ID for the buy model |
| `long.tiers` | array | ✅ | List of tier objects (see below) |
| `short` | object | ✅ | Short-side configuration (same shape as `long`) |

**Tier Object:**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `min_prob` | float | `0.0` | Min probability to match this tier |
| `lots` | int | `1` | Position size |
| `tp_atr_mult` | float | _engine global_ | Per-trade TP override |
| `sl_atr_mult` | float | _engine global_ | Per-trade SL override |
| `trailing_atr_mult` | float | _engine global_ | Per-trade trailing override |
| `max_hold_bars` | int | _engine global_ | Per-trade time barrier override |
| `label` | string | `""` | Human-readable label for logging |

### Tier Matching Rules

1. Tiers are sorted **highest `min_prob` first**; first match wins
2. If no tier matches, **HOLD** is returned
3. When both buy and sell fire on the same bar → **higher probability wins**
4. When already in a position → no new entries (conservative/no-flip)

### Per-Order Overrides

When a tier is matched, its `tp_atr_mult`, `sl_atr_mult`, `trailing_atr_mult`, and `max_hold_bars` values are attached to the returned `Order`. The engine uses these per-trade values instead of the global config. If a tier omits a field, the engine's global default is used.

---

## Example Configs

### Single-Model
```json
{
    "nickname": "Manatee",
    "direction": "LONG",
    "entry_threshold": 0.60,
    "tp_atr_mult": 3.0,
    "sl_atr_mult": 1.5,
    "trailing_atr_mult": 1.0,
    "cooldown_bars": 10,
    "max_hold_bars": 288,
    "allow_concurrent": false,
    "max_concurrent": 1,
    "sizing_tiers": {"0.80": 3, "0.70": 3, "0.60": 1, "0.50": 1},
    "live_config": {
        "experiment_id": "EXP-017_S_Ultimate",
        "client_id": 10,
        "entry_mode": "adaptive",
        "adaptive_priority": "Normal"
    }
}
```

### Ensemble
```json
{
    "nickname": "ManateeKoala_Conservative",
    "execution_class": "ConservativeEnsembleStrategy",
    "models": {
        "long": {"experiment_id": "EXP-017_S_Ultimate", "threshold": 0.70},
        "short": {"experiment_id": "EXP-020_S_Ultimate_Short", "threshold": 0.60}
    },
    "tp_atr_mult": 7.0,
    "sl_atr_mult": 1.0,
    "trailing_atr_mult": 1.0,
    "cooldown_bars": 10,
    "max_hold_bars": 288,
    "allow_concurrent": false,
    "max_concurrent": 1,
    "live_config": {
        "client_id": 13
    }
}
```

### TieredEnsemble
```json
{
    "nickname": "TieredEnsemble2",
    "execution_class": "TieredEnsembleStrategy",
    "long": {
        "experiment_id": "EXP-025_S_Ultimate_OOS",
        "tiers": [
            {"min_prob": 0.75, "lots": 2, "tp_atr_mult": 3.0, "sl_atr_mult": 1.0, "trailing_atr_mult": 2.0, "max_hold_bars": 288, "label": "high_confidence"},
            {"min_prob": 0.60, "lots": 1, "tp_atr_mult": 1.5, "sl_atr_mult": 1.5, "max_hold_bars": 144, "label": "base"}
        ]
    },
    "short": {
        "experiment_id": "EXP-026_S_Ultimate_Short_OOS",
        "tiers": [
            {"min_prob": 0.80, "lots": 3, "tp_atr_mult": 1.5, "sl_atr_mult": 3.0, "trailing_atr_mult": 4.0, "max_hold_bars": 144, "label": "high_confidence"},
            {"min_prob": 0.60, "lots": 1, "tp_atr_mult": 1.0, "sl_atr_mult": 2.0, "max_hold_bars": 144, "label": "base"}
        ]
    },
    "tp_atr_mult": 2.0,
    "sl_atr_mult": 1.0,
    "max_hold_bars": 288,
    "cooldown_bars": 10,
    "live_config": {"client_id": 14}
}
```

---

## Client ID Assignments

| Config | client_id |
|--------|-----------|
| manatee.json | 10 |
| koala.json | 11 |
| manatee_single.json | 12 |
| ensemble_conservative.json | 13 |
| TieredEnsemble2.json | 14 |
