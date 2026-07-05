"""
TDD-TESTER AUTHORIZATION
Target Implementation File: src/live_execution/instrument_context.py (NEW),
                            src/live_execution/live_trader.py (lines ~276-278),
                            src/live_execution/cli.py (fail-fast pre-connect),
                            configs/strategies/ensemble2_opt.json (migration)
Target Class/Function: InstrumentContext, resolve_instrument_context,
                       derive_model_symbol, LiveTrader.__init__, cli.main
Status: FINALIZED
Strict-Lock: TRUE (Implementation agents may NOT modify this file)

Ticket: t1-instrument-metadata_07042026_1543
Phase: RED — the module src.live_execution.instrument_context does NOT exist
yet; this file fails at import (ghost import per TDD protocol) until the
Coder implements audit.md section 4.2-4.5.

Coverage (audit.md section 6, tests 6-17 + impact_review conditions C2/C4):
  - resolve_instrument_context: missing / empty / unknown execution_symbol
    raise with the audit section 4.2 wording; CL passthrough; lowercase
    normalization (C2); MCL Two-Stream brain mapping; brain_symbol override.
  - derive_model_symbol opportunistic tag parsing (full census table).
  - Model-tag cross-check: E2E_ES_* vs exec CL raises; symbol-less tags skip;
    MCL + E2E_CL_* passes; explicit models.<side>.symbol hard-enforced.
  - Shipped-config integration: ES01B intended failure (C4), HS14B happy
    path, ensemble2_opt migration, full configs/strategies glob.
  - LiveTrader wiring: missing execution_symbol raises at __init__;
    HS14B-style config sets self._execution_symbol == "CL" unchanged.
  - cli.main fail-fast: resolver ValueError raised BEFORE any factory
    constructs a client.

Error-message assertions use stable substrings of the exact audit section 4.2
wording, not full strings.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.core.instrument_master import INSTRUMENT_REGISTRY
from src.live_execution.live_trader import LiveTrader

# Ghost import — the Coder creates this module (audit section 4.2).
from src.live_execution.instrument_context import (  # noqa: E402
    InstrumentContext,
    derive_model_symbol,
    resolve_instrument_context,
)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_DIR = _PROJECT_ROOT / "configs" / "strategies"

# Stable substrings of the audit section 4.2 exact error messages
_MSG_MISSING = "missing required field 'execution_symbol'"
_MSG_UNKNOWN = "is not in INSTRUMENT_REGISTRY"
_MSG_BRAIN_INCOMPATIBLE = "is not compatible with execution_symbol"
_MSG_MODEL_MISMATCH = "does not match model symbol"


def _load_config(name: str) -> dict:
    return json.loads((_CONFIG_DIR / name).read_text(encoding="utf-8"))


class DummyStrategy:
    """Minimal strategy stub mirroring tests/test_live_macro_refresh.py."""

    def __init__(self, feature_names: list[str] | None = None, config: dict | None = None):
        self.feature_names = feature_names if feature_names is not None else ["MACD"]
        self.name = "DummyStrategy"
        self.direction = "LONG"
        self.config = config if config is not None else {}


# ===========================================================================
# resolve_instrument_context — hard-raise rules (audit tests 6-7)
# ===========================================================================

class TestResolverHardFailures:
    def test_missing_execution_symbol_raises(self):
        with pytest.raises(ValueError, match=_MSG_MISSING):
            resolve_instrument_context({})

    def test_missing_execution_symbol_raises_with_nickname(self):
        """Nickname is display-only and allowed to soft-default; the raise
        must still fire and reference the missing field."""
        with pytest.raises(ValueError, match=_MSG_MISSING):
            resolve_instrument_context({"nickname": "NoSymbolConfig"})

    def test_empty_execution_symbol_raises(self):
        with pytest.raises(ValueError, match=_MSG_MISSING):
            resolve_instrument_context({"execution_symbol": ""})

    def test_non_string_execution_symbol_raises(self):
        with pytest.raises(ValueError, match=_MSG_MISSING):
            resolve_instrument_context({"execution_symbol": None})

    def test_unknown_symbol_raises(self):
        with pytest.raises(ValueError, match=_MSG_UNKNOWN):
            resolve_instrument_context({"execution_symbol": "XX"})


# ===========================================================================
# resolve_instrument_context — happy paths (audit tests 8-10, condition C2)
# ===========================================================================

class TestResolverHappyPaths:
    def test_cl_passthrough(self):
        ctx = resolve_instrument_context({"execution_symbol": "CL"})
        assert isinstance(ctx, InstrumentContext)
        assert ctx.execution_symbol == "CL"
        assert ctx.brain_symbol == "CL"
        assert ctx.execution_instrument.exchange == "NYMEX"
        assert ctx.brain_instrument is ctx.execution_instrument

    def test_lowercase_cl_normalizes(self):
        """C2: resolver preserves the legacy .upper() normalization —
        a lowercase 'cl' config remains accepted exactly as today."""
        ctx = resolve_instrument_context({"execution_symbol": "cl"})
        assert ctx.execution_symbol == "CL"
        assert ctx.brain_symbol == "CL"

    def test_es_brain_is_self(self):
        ctx = resolve_instrument_context({"execution_symbol": "ES"})
        assert ctx.execution_symbol == "ES"
        assert ctx.brain_symbol == "ES"

    def test_mcl_brain_maps_to_cl(self):
        """Two-Stream: exec MCL (hands) -> brain CL via micro_of derivation."""
        ctx = resolve_instrument_context({"execution_symbol": "MCL"})
        assert ctx.execution_symbol == "MCL"
        assert ctx.brain_symbol == "CL"
        assert ctx.execution_instrument.symbol == "MCL"
        assert ctx.brain_instrument.symbol == "CL"

    def test_brain_symbol_explicit_valid_override(self):
        ctx = resolve_instrument_context(
            {"execution_symbol": "MCL", "brain_symbol": "CL"}
        )
        assert ctx.brain_symbol == "CL"

    def test_brain_symbol_incompatible_raises(self):
        with pytest.raises(ValueError, match=_MSG_BRAIN_INCOMPATIBLE):
            resolve_instrument_context(
                {"execution_symbol": "CL", "brain_symbol": "ES"}
            )


# ===========================================================================
# derive_model_symbol — opportunistic tag parsing (audit test 11)
# ===========================================================================

class TestDeriveModelSymbol:
    @pytest.mark.parametrize(
        "experiment_id,expected",
        [
            ("E2E_CL_HourSet_14B_long_average_precision", "CL"),
            ("E2E_ES_HourSet_01B_long_logloss", "ES"),
            # symbol-less post-T6 shape: "HourSet" is not a registry symbol
            ("E2E_HourSet_08_long_logloss", None),
            ("E2E_HourSet_01B_long_logloss", None),
            # legacy shapes
            ("EXP-025_S_Ultimate_OOS", None),
            ("sweep_h4s01_3x1_96h", None),
            # degenerate
            ("_long_logloss", None),
            ("", None),
            (None, None),
            # case normalization
            ("e2e_cl_HourSet_14B_long_logloss", "CL"),
        ],
    )
    def test_derive_model_symbol_table(self, experiment_id, expected):
        assert derive_model_symbol(experiment_id) == expected


# ===========================================================================
# Model-tag cross-check through the resolver (audit tests 12-14)
# ===========================================================================

class TestModelSymbolCrossCheck:
    def test_model_symbol_mismatch_raises(self):
        """Reproduces the shipped ES01B bug: exec CL + E2E_ES_* models."""
        cfg = {
            "nickname": "BadES",
            "execution_symbol": "CL",
            "models": {
                "long": {
                    "experiment_id": "E2E_ES_HourSet_01B_long_logloss",
                    "threshold": 0.53,
                },
            },
        }
        with pytest.raises(ValueError, match=_MSG_MODEL_MISMATCH):
            resolve_instrument_context(cfg)

    def test_symbolless_experiment_id_does_not_raise(self):
        """Opportunistic check: no registry-symbol token after E2E_ -> skip."""
        cfg = {
            "nickname": "SymbolLess",
            "execution_symbol": "CL",
            "models": {
                "long": {
                    "experiment_id": "E2E_HourSet_08_long_logloss",
                    "threshold": 0.55,
                },
                "short": {
                    "experiment_id": "E2E_HourSet_08_short_logloss",
                    "threshold": 0.55,
                },
            },
        }
        ctx = resolve_instrument_context(cfg)
        assert ctx.execution_symbol == "CL"

    def test_model_symbol_match_micro(self):
        """MCL (hands) + CL-trained models (brain) is the supported
        Two-Stream pattern and must pass."""
        cfg = {
            "nickname": "MicroTwoStream",
            "execution_symbol": "MCL",
            "models": {
                "long": {
                    "experiment_id": "E2E_CL_HourSet_14B_long_average_precision",
                    "threshold": 0.56,
                },
            },
        }
        ctx = resolve_instrument_context(cfg)
        assert ctx.brain_symbol == "CL"

    def test_explicit_model_symbol_field_enforced(self):
        """T6 forward-compat: explicit models.<side>.symbol is HARD-enforced
        even when the experiment_id carries no symbol tag."""
        cfg = {
            "nickname": "ExplicitSymbol",
            "execution_symbol": "CL",
            "models": {
                "long": {
                    "symbol": "ES",
                    "experiment_id": "E2E_HourSet_01B_long_logloss",
                    "threshold": 0.53,
                },
            },
        }
        with pytest.raises(ValueError):
            resolve_instrument_context(cfg)

    def test_explicit_model_symbol_field_matching_passes(self):
        cfg = {
            "nickname": "ExplicitSymbolOK",
            "execution_symbol": "CL",
            "models": {
                "long": {
                    "symbol": "CL",
                    "experiment_id": "E2E_HourSet_01B_long_logloss",
                    "threshold": 0.53,
                },
            },
        }
        ctx = resolve_instrument_context(cfg)
        assert ctx.brain_symbol == "CL"


# ===========================================================================
# Shipped-config integration (audit test 15, condition C4)
# ===========================================================================

class TestShippedConfigs:
    def test_es01b_shipped_config_raises_intended_failure(self):
        """INTENDED FAILURE (impact_review C4) — asserts DESIRED post-T1
        behavior: the shipped ES01B config declares execution_symbol "CL"
        but its models are E2E_ES_* — exactly the mis-generated config T1
        exists to catch. It must REFUSE to resolve until T6 regenerates it.
        """
        cfg = _load_config("ES01B_Sharpe_E03_07042026.json")
        assert cfg["execution_symbol"] == "CL"  # precondition: bug still shipped
        with pytest.raises(ValueError, match=_MSG_MODEL_MISMATCH):
            resolve_instrument_context(cfg)

    def test_hs14b_shipped_config_resolves(self):
        """Happy-path pin: the live prod config resolves unchanged."""
        cfg = _load_config("HS14B_Sharpe_E01_06262026.json")
        ctx = resolve_instrument_context(cfg)
        assert ctx.execution_symbol == "CL"
        assert ctx.brain_symbol == "CL"
        assert ctx.execution_instrument.exchange == "NYMEX"

    def test_ensemble2_opt_migrated_and_resolves(self):
        """ensemble2_opt.json was the only config missing execution_symbol;
        the Coder migrates it (adds "execution_symbol": "CL"). Fails RED
        because the field is absent today; passes once migrated."""
        cfg = _load_config("ensemble2_opt.json")
        assert cfg.get("execution_symbol") == "CL", (
            "ensemble2_opt.json must be migrated to carry "
            '"execution_symbol": "CL" (audit section 4.5)'
        )
        ctx = resolve_instrument_context(cfg)
        assert ctx.execution_symbol == "CL"
        assert ctx.brain_symbol == "CL"

    def test_all_shipped_configs_resolve_except_es01b(self):
        """Fleet-wide gate: every config in configs/strategies/ resolves,
        with the single documented exception of the mis-generated ES01B
        (which must raise until T6 regenerates it)."""
        config_paths = sorted(_CONFIG_DIR.glob("*.json"))
        assert config_paths, f"No strategy configs found under {_CONFIG_DIR}"

        intended_failures = {"ES01B_Sharpe_E03_07042026.json"}
        for path in config_paths:
            cfg = json.loads(path.read_text(encoding="utf-8"))
            if path.name in intended_failures:
                with pytest.raises(ValueError):
                    resolve_instrument_context(cfg)
            else:
                ctx = resolve_instrument_context(cfg)
                assert ctx.execution_symbol in INSTRUMENT_REGISTRY, (
                    f"{path.name}: resolved to unknown symbol "
                    f"{ctx.execution_symbol!r}"
                )


# ===========================================================================
# LiveTrader wiring (audit test 16) — construction seam mirrors
# tests/test_live_macro_refresh.py (direct __init__ with mocked clients).
# NOTE: tests/test_live_macro_refresh.py fixtures themselves are updated by
# the Coder, not here.
# ===========================================================================

class TestLiveTraderWiring:
    def test_live_trader_missing_execution_symbol_raises(self):
        """LiveTrader.__init__ must hard-raise (no silent CL default) when
        the strategy config lacks execution_symbol."""
        with pytest.raises(ValueError, match=_MSG_MISSING):
            LiveTrader(
                data_client=MagicMock(),
                exec_client=MagicMock(),
                strategy=DummyStrategy(config={}),
                dry_run=True,
            )

    def test_live_trader_sets_execution_symbol_from_config(self):
        """HS14B-style config (execution_symbol CL) constructs unchanged and
        keeps the legacy attribute name/type: self._execution_symbol == 'CL'."""
        cfg = {
            "nickname": "HS14B_style",
            "execution_symbol": "CL",
            "bar_size": "1h",
            "models": {
                "long": {
                    "experiment_id": "E2E_CL_HourSet_14B_long_average_precision",
                    "threshold": 0.56,
                },
            },
        }
        trader = LiveTrader(
            data_client=MagicMock(),
            exec_client=MagicMock(),
            strategy=DummyStrategy(config=cfg),
            dry_run=True,
        )
        assert trader._execution_symbol == "CL"
        assert isinstance(trader._execution_symbol, str)

    def test_live_trader_lowercase_symbol_normalized(self):
        """C2: lowercase 'cl' in a config still normalizes to 'CL'."""
        trader = LiveTrader(
            data_client=MagicMock(),
            exec_client=MagicMock(),
            strategy=DummyStrategy(config={"execution_symbol": "cl"}),
            dry_run=True,
        )
        assert trader._execution_symbol == "CL"


# ===========================================================================
# CLI fail-fast (audit test 17): resolver ValueError must fire BEFORE any
# data/exec factory constructs a client (no IBKR object may exist).
# ===========================================================================

class TestCliFailFast:
    def test_cli_fails_fast_pre_connect(self):
        from src.live_execution import cli

        stub_strategy = SimpleNamespace(
            config={"nickname": "NoSymbolConfig"},  # missing execution_symbol
            feature_names=[],
            name="StubStrategy",
            direction="LONG",
        )

        with patch(
            "src.live_execution.strategies.configurable_strategy.ConfigurableStrategy",
            return_value=stub_strategy,
        ), patch(
            "src.live_execution.factories.DataFeedFactory.create"
        ) as mock_data_create, patch(
            "src.live_execution.factories.ExecutionFactory.create"
        ) as mock_exec_create, patch(
            "src.live_execution.live_trader.LiveTrader"
        ) as mock_trader_cls, patch(
            "src.live_execution.cli._setup_file_logging"
        ), patch(
            "sys.argv", ["cli", "--config", "dummy_config.json"]
        ):
            with pytest.raises(ValueError, match=_MSG_MISSING):
                cli.main()

        mock_data_create.assert_not_called()
        mock_exec_create.assert_not_called()
        mock_trader_cls.assert_not_called()
