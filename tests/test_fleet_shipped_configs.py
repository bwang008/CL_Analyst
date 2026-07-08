"""Fleet-wide shipped-config invariants — parametrized over the live fleet
manifest (``configs/fleet/fleet_manifest.json``), the source of truth for what
actually ships.

Replaces the per-model ES01B sentinel pins removed under ticket
``es-config-drift-repin_07062026_2124``. Those hardcoded a single model
filename (``ES01B_Sharpe_E03_07042026.json``) and rotted the moment the fleet
swapped a model — commit a7a0b7d swapped ES Sharpe_E03 -> Sortino_E01 and left
10 red pins referencing a deleted file. Iterating the manifest instead keeps
the *behavioral* guarantees (every shipped config resolves to a tradeable
instrument and its model artifacts exist on disk) while (a) never breaking on an
intentional model swap and (b) covering the whole fleet — CL/ES/NG/GC/SI — not
just ES. It is also robust to the execution-symbol full<->micro conversions the
fleet configs carry (e.g. ES<->MES): it asserts a *valid resolved instrument*,
not a specific symbol.

NOTE: ``models.*.predictions_path`` existence is deliberately NOT asserted here.
It is a backtest/provenance artifact never read by live execution, and most live
configs currently point at un-materialized CSVs; that gap is owned by ticket
``predictions-path-provenance_07062026_2124``.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.live_execution.instrument_context import resolve_instrument_context

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_FLEET_MANIFEST = _PROJECT_ROOT / "configs" / "fleet" / "fleet_manifest.json"


def _enabled_fleet_configs() -> list[tuple[str, Path]]:
    """(nickname-ish name, absolute path) for every ENABLED manifest instance."""
    manifest = json.loads(_FLEET_MANIFEST.read_text(encoding="utf-8"))
    out: list[tuple[str, Path]] = []
    for inst in manifest["instances"]:
        if not inst.get("enabled", True):
            continue
        rel = inst["config"]
        out.append((Path(rel).stem, _PROJECT_ROOT / rel))
    return out


_FLEET_CONFIGS = _enabled_fleet_configs()
_FLEET_PATHS = [path for _, path in _FLEET_CONFIGS]
_FLEET_IDS = [name for name, _ in _FLEET_CONFIGS]


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_fleet_manifest_has_enabled_instances():
    """Guard the parametrization itself: an empty/renamed manifest must fail
    loudly, not silently reduce every shipped-config check below to zero cases."""
    assert _FLEET_CONFIGS, (
        f"no enabled instances found in {_FLEET_MANIFEST} — the fleet-wide "
        "shipped-config checks would silently cover nothing"
    )


@pytest.mark.parametrize("cfg_path", _FLEET_PATHS, ids=_FLEET_IDS)
def test_shipped_config_file_exists(cfg_path):
    """Every config the manifest references must exist on disk. A manifest
    pointing at a deleted config is the exact a7a0b7d-class regression this
    module exists to catch."""
    assert cfg_path.is_file(), (
        f"fleet manifest references a config that is missing on disk: {cfg_path}"
    )


@pytest.mark.parametrize("cfg_path", _FLEET_PATHS, ids=_FLEET_IDS)
def test_shipped_config_resolves_to_tradeable_instrument(cfg_path):
    """Each shipped config must resolve through the live instrument resolver to
    a fully-specified, tradeable contract. Catches a config that would size or
    price live orders wrong (bad/unknown symbol, non-positive multiplier/tick)
    and is agnostic to full<->micro execution-symbol choices."""
    ctx = resolve_instrument_context(_load(cfg_path))
    assert ctx.execution_symbol, "empty execution_symbol after resolution"
    assert ctx.brain_symbol, "empty brain_symbol after resolution"
    assert ctx.execution_instrument.exchange, "empty execution exchange"
    assert ctx.execution_instrument.multiplier > 0, (
        f"non-positive multiplier {ctx.execution_instrument.multiplier!r}"
    )
    assert ctx.execution_instrument.tick_size > 0, (
        f"non-positive tick_size {ctx.execution_instrument.tick_size!r}"
    )


@pytest.mark.parametrize("cfg_path", _FLEET_PATHS, ids=_FLEET_IDS)
def test_shipped_config_model_artifacts_exist(cfg_path):
    """Both sides' ``model_path`` must exist on disk. Nothing in the fleet
    launch preflight (fleet_runner.validate_data_prerequisites) checks the model
    ``.pkl``; a missing model_path otherwise only surfaces as a child crash
    mid-launch, when configurable_strategy lazily loads it."""
    cfg = _load(cfg_path)
    for side in ("long", "short"):
        entry = cfg["models"][side]
        model_path = _PROJECT_ROOT / entry["model_path"]
        assert model_path.is_file(), (
            f"models.{side}.model_path missing on disk: {entry['model_path']}"
        )


@pytest.mark.parametrize("cfg_path", _FLEET_PATHS, ids=_FLEET_IDS)
def test_shipped_config_does_not_disable_5m_stream(cfg_path):
    """Anti-drift fence generalized from the removed ES01B enable_5m_stream pin:
    the seedless-5m design dropped ``live_config.enable_5m_stream`` so hourly
    models ride the default-true shallow-bootstrap 5m path. A config silently
    re-adding it as ``false`` would kill that stream. Absent or true is fine;
    present-and-false is the regression."""
    live = _load(cfg_path).get("live_config", {})
    assert live.get("enable_5m_stream", True) is not False, (
        f"{cfg_path.stem} carries live_config.enable_5m_stream=false — this "
        "silently disables the default 5m shallow-bootstrap stream"
    )
