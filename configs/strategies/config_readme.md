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

---

## Shared Attributes

### Identity

| Attribute | Type | Default | Description |
|-----------|------|---------|-------------|
| `nickname` | string | `"unnamed"` | Human-readable name for logging and reports |

### Execution Strategy

| Attribute | Type | Default | Description |
|-----------|------|---------|-------------|
| `execution_class` | string | `"SingleModelStrategy"` | Strategy class: `SingleModelStrategy`, `ConservativeEnsembleStrategy`, `AggressiveEnsembleStrategy` |
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

> [!NOTE]
> For backward compatibility, `experiment_id` is also read from the top-level config if `live_config.experiment_id` is not present.

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
        "client_id": 10
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

---

## Client ID Assignments

| Config | client_id |
|--------|-----------|
| manatee.json | 10 |
| koala.json | 11 |
| manatee_single.json | 12 |
| ensemble_conservative.json | 13 |
