"""
Instrument resolution + fail-fast validation for the live engine.

T1 of the multi-symbol live-gaps program
(ticket t1-instrument-metadata_07042026_1543, audit.md section 4.2).

House rule: NO silent defaults. A strategy config must declare its
instrument explicitly via ``execution_symbol``; missing/unknown/mismatched
symbols raise with actionable messages before any IBKR object exists.

Leaf-safe: imports only stdlib + ``src.core.instrument_master`` (itself a
pure stdlib leaf), so ``cli.py`` and ``live_trader.py`` can import this
module top-level without cycles.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from src.core.instrument_master import INSTRUMENT_REGISTRY, Instrument

log = logging.getLogger("LiveTrader")


@dataclass(frozen=True)
class InstrumentContext:
    """Resolved instrument pair for a live strategy.

    execution_symbol: the contract actually traded ("hands", e.g. MCL).
    brain_symbol: the symbol the models were trained on (micro parent when
        execution is a micro; otherwise the execution symbol itself).
    """

    execution_symbol: str
    brain_symbol: str
    execution_instrument: Instrument
    brain_instrument: Instrument


def derive_model_symbol(experiment_id: Optional[str]) -> Optional[str]:
    """Opportunistically parse the symbol token from an experiment_id.

    Only the token immediately after ``E2E_`` counts, and only when it is a
    registry symbol (audit section 3a): ``E2E_CL_HourSet_14B_...`` -> "CL";
    ``E2E_HourSet_08_...`` -> None (post-T6 symbol-stripped shape); legacy
    shapes -> None.
    """
    parts = (experiment_id or "").split("_")
    if len(parts) >= 2 and parts[0].upper() == "E2E" and parts[1].upper() in INSTRUMENT_REGISTRY:
        return parts[1].upper()
    return None


def validate_models_against_symbol(
    strategy_config: Mapping[str, Any], brain_symbol: str
) -> None:
    """Cross-check every model entry's symbol against the brain symbol.

    - Explicit ``models.<side>.symbol`` (T6 forward-compat) is HARD-enforced.
    - Otherwise the experiment_id tag is checked opportunistically; tag-free
      ids are skipped with an INFO log (audit section 3a census).
    """
    nickname = strategy_config.get("nickname", "<unnamed>")
    models = strategy_config.get("models", {})
    if not isinstance(models, Mapping):
        return
    exec_symbol = strategy_config.get("execution_symbol", brain_symbol)
    for side, entry in models.items():
        if not isinstance(entry, Mapping):
            continue
        exp_id = entry.get("experiment_id")
        explicit_symbol = entry.get("symbol")
        if isinstance(explicit_symbol, str) and explicit_symbol:
            tag = explicit_symbol.upper()
            declared_by = f"models.{side}.symbol '{explicit_symbol}'"
        else:
            tag = derive_model_symbol(exp_id)
            declared_by = f"models.{side}.experiment_id '{exp_id}'"
            if tag is None:
                log.info(
                    "models.%s.experiment_id '%s' carries no symbol tag — "
                    "skipping symbol cross-check",
                    side, exp_id,
                )
                continue
        if tag != brain_symbol:
            raise ValueError(
                f"Strategy config '{nickname}': execution_symbol "
                f"'{exec_symbol}' (brain '{brain_symbol}') does not match "
                f"model symbol '{tag}' declared by {declared_by}. Refusing "
                f"to start — this config would trade the wrong instrument."
            )


def resolve_instrument_context(
    strategy_config: Mapping[str, Any]
) -> InstrumentContext:
    """Resolve and validate the instrument for a strategy config.

    Raises ValueError (audit section 4.2 exact wording) on:
      1. missing/empty/non-string ``execution_symbol`` (no silent CL default),
      2. symbol not in INSTRUMENT_REGISTRY,
      3. explicit ``brain_symbol`` incompatible with the execution symbol,
      4. model symbol tag mismatch (via validate_models_against_symbol).
    """
    nickname = strategy_config.get("nickname", "<unnamed>")

    raw_symbol = strategy_config.get("execution_symbol")
    if not isinstance(raw_symbol, str) or not raw_symbol.strip():
        raise ValueError(
            f"Strategy config '{nickname}' is missing required field "
            f"'execution_symbol'. Every live strategy config must declare "
            f"its instrument explicitly (no silent CL default). "
            f'Add e.g. "execution_symbol": "CL".'
        )

    # C2: preserve legacy .upper() normalization (lowercase "cl" accepted).
    execution_symbol = raw_symbol.strip().upper()
    if execution_symbol not in INSTRUMENT_REGISTRY:
        raise ValueError(
            f"Strategy config '{nickname}': execution_symbol "
            f"'{execution_symbol}' is not in INSTRUMENT_REGISTRY "
            f"(known: {sorted(INSTRUMENT_REGISTRY.keys())})."
        )
    execution_instrument = INSTRUMENT_REGISTRY[execution_symbol]

    micro_parent = execution_instrument.micro_of
    raw_brain = strategy_config.get("brain_symbol")
    if raw_brain is not None:
        if not isinstance(raw_brain, str) or not raw_brain.strip():
            raise ValueError(
                f"Strategy config '{nickname}': brain_symbol "
                f"{raw_brain!r} is not compatible with execution_symbol "
                f"'{execution_symbol}' (expected '{execution_symbol}'"
                + (f" or micro parent '{micro_parent}'" if micro_parent else "")
                + ")."
            )
        brain_symbol = raw_brain.strip().upper()
        if brain_symbol not in (execution_symbol, micro_parent):
            raise ValueError(
                f"Strategy config '{nickname}': brain_symbol "
                f"'{brain_symbol}' is not compatible with execution_symbol "
                f"'{execution_symbol}' (expected '{execution_symbol}'"
                + (f" or micro parent '{micro_parent}'" if micro_parent else "")
                + ")."
            )
    else:
        # Structural derivation (not a config default): a micro's brain is
        # its parent contract; an outright is its own brain.
        brain_symbol = micro_parent or execution_symbol

    brain_instrument = INSTRUMENT_REGISTRY[brain_symbol]

    validate_models_against_symbol(strategy_config, brain_symbol)

    return InstrumentContext(
        execution_symbol=execution_symbol,
        brain_symbol=brain_symbol,
        execution_instrument=execution_instrument,
        brain_instrument=brain_instrument,
    )
