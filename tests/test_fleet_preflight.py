"""Fleet data preflight (ticket fleet-seed-preflight-gap_07052026_2215).

NEW test file by design: tests/test_fleet_runner.py is Strict-Locked and
exercises validate() on minimal symbol-less fixtures — the preflight lives in
a separate method (validate_data_prerequisites) precisely so those stay green.

Covers the 2026-07-05 incident class (NG/GC crash-looped on missing 1h seeds)
plus the user-ruled scenarios: corrupted-cache-needs-deletion, first-start
(seed only, no cache), and stale-seed-beyond-IBKR-backfill-horizon.
"""
import json

import pandas as pd
import pytest

from src.live_execution.data_manager import (
    MAX_BACKFILL_GAP_DAYS_1H,
    MAX_BACKFILL_GAP_DAYS_5M,
    required_live_data_artifacts,
)
from src.live_execution.fleet_runner import FleetRunner


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _write_manifest(tmp_path, configs):
    manifest = {
        "instances": [
            {"config": str(c), "enabled": True, "extra_args": []}
            for c in configs
        ],
        "stagger_seconds": 0,
        "data_port": 4002,
        "exec_port": 4002,
    }
    path = tmp_path / "fleet_manifest.json"
    path.write_text(json.dumps(manifest))
    return path


def _write_config(tmp_path, name, client_id):
    cfg = {"strategy_name": name, "live_config": {"client_id": client_id}}
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(cfg))
    return path


def _write_bars_parquet(path, end_ts, n=100, freq="h", as_column=True):
    idx = pd.date_range(end=end_ts, periods=n, freq=freq)
    df = pd.DataFrame({
        "Open": 1.0, "High": 1.5, "Low": 0.5, "Close": 1.0, "Volume": 10,
    }, index=idx)
    if as_column:
        df = df.reset_index().rename(columns={"index": "DateTime"})
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path)
    return path


def _runner(tmp_path, n_configs=1, reqs_by_name=None, features_by_name=None):
    """A validated runner whose strategy loading + requirements are injected.

    The preflight's orchestration (per-instance loop, extra_args overrides,
    error aggregation, fail-before-spawn) runs for real; only the heavy
    model/config resolution is faked.
    """
    configs = [
        _write_config(tmp_path, f"model{i}", 1000 + 10 * i)
        for i in range(n_configs)
    ]
    manifest = _write_manifest(tmp_path, configs)
    spawned = []
    runner = FleetRunner(
        manifest_path=manifest,
        popen=lambda cmd, **kw: spawned.append(cmd),
        sleep=lambda s: None,
    )
    runner.load_manifest()
    runner.validate()
    runner._spawned_probe = spawned

    reqs_by_name = reqs_by_name or {}
    features_by_name = features_by_name or {}

    def fake_load(config_path):
        name = config_path.stem if hasattr(config_path, "stem") else str(config_path)
        name = name.split("\\")[-1].split("/")[-1].replace(".json", "")
        return {"_name": name}, features_by_name.get(name, ["RSI_14"])

    def fake_reqs(cfg):
        return [dict(r) for r in reqs_by_name.get(cfg["_name"], [])]

    runner._load_strategy = fake_load
    import src.live_execution.fleet_runner as fr_mod
    return runner, fake_reqs, fr_mod


@pytest.fixture
def patch_reqs(monkeypatch):
    """Patch the requirements authority inside the preflight's local import."""
    def _apply(fake):
        import src.live_execution.data_manager as dm
        monkeypatch.setattr(dm, "required_live_data_artifacts", fake)
    return _apply


NOW = pd.Timestamp.now().tz_localize(None)
FRESH = NOW - pd.Timedelta(hours=3)
STALE_1H = NOW - pd.Timedelta(days=MAX_BACKFILL_GAP_DAYS_1H + 10)


# ---------------------------------------------------------------------------
# required_live_data_artifacts — the shared authority (real function)
# ---------------------------------------------------------------------------

class TestRequiredArtifacts:
    def _cfg(self, **over):
        cfg = {
            "execution_symbol": "ES",
            "bar_size": "1h",
            "models": {},
            "live_config": {"client_id": 2000},
        }
        cfg.update(over)
        return cfg

    def test_hourly_requires_1h_only(self):
        reqs = required_live_data_artifacts(self._cfg())
        assert [r["stream"] for r in reqs] == ["1h"]
        assert reqs[0]["seed"].name == "ES_raw_1h.parquet"
        assert reqs[0]["cache"].name == "warm_start_cache_ES_1h.parquet"
        assert reqs[0]["max_gap_days"] == MAX_BACKFILL_GAP_DAYS_1H

    def test_5m_model_requires_5m_seed(self):
        reqs = required_live_data_artifacts(self._cfg(bar_size="5m"))
        assert [r["stream"] for r in reqs] == ["5m"]
        assert reqs[0]["max_gap_days"] == MAX_BACKFILL_GAP_DAYS_5M

    def test_bar_size_defaults_to_5m(self):
        cfg = self._cfg()
        del cfg["bar_size"]
        reqs = required_live_data_artifacts(cfg)
        assert [r["stream"] for r in reqs] == ["5m"]

    def test_seed_path_1h_override_absolute(self, tmp_path):
        override = tmp_path / "custom_seed.parquet"
        cfg = self._cfg(live_config={"client_id": 2000,
                                     "seed_path_1h": str(override)})
        reqs = required_live_data_artifacts(cfg)
        assert reqs[0]["seed"] == override

    def test_seed_path_1h_override_relative_resolves_against_data_root(self):
        from src.data_paths import get_data_root
        cfg = self._cfg(live_config={"client_id": 2000,
                                     "seed_path_1h": "processed/x.parquet"})
        reqs = required_live_data_artifacts(cfg)
        assert reqs[0]["seed"] == get_data_root() / "processed/x.parquet"

    def test_enable_5m_false_with_5m_bar_size_raises(self):
        cfg = self._cfg(bar_size="5m",
                        live_config={"client_id": 2000,
                                     "enable_5m_stream": False})
        with pytest.raises(ValueError, match="enable_5m_stream=false"):
            required_live_data_artifacts(cfg)

    def test_micro_maps_to_brain_symbol(self):
        reqs = required_live_data_artifacts(self._cfg(execution_symbol="MES"))
        assert reqs[0]["seed"].name == "ES_raw_1h.parquet"


# ---------------------------------------------------------------------------
# validate_data_prerequisites — orchestration + per-scenario behavior
# ---------------------------------------------------------------------------

class TestPreflight:
    def _run(self, tmp_path, monkeypatch, reqs, features=None, extra_args=None,
             n_configs=1):
        runner, _, _ = _runner(
            tmp_path, n_configs=n_configs,
            features_by_name=features or {},
        )
        if extra_args is not None:
            runner.instances[0].extra_args = extra_args
        import src.live_execution.data_manager as dm
        monkeypatch.setattr(
            dm, "required_live_data_artifacts",
            lambda cfg: [dict(r) for r in reqs.get(cfg["_name"], [])],
        )
        return runner

    def test_missing_seed_and_cache_raises_before_spawn(self, tmp_path, monkeypatch):
        req = {"stream": "1h", "cache": tmp_path / "no_cache.parquet",
               "seed": tmp_path / "no_seed.parquet",
               "max_gap_days": MAX_BACKFILL_GAP_DAYS_1H}
        runner = self._run(tmp_path, monkeypatch, {"model0": [req]})
        with pytest.raises(RuntimeError, match="neither 1h cache nor seed"):
            runner.validate_data_prerequisites()
        assert runner._spawned_probe == []  # nothing launched

    def test_seed_only_first_start_passes(self, tmp_path, monkeypatch):
        seed = _write_bars_parquet(tmp_path / "ES_raw_1h.parquet", FRESH)
        req = {"stream": "1h", "cache": tmp_path / "absent_cache.parquet",
               "seed": seed, "max_gap_days": MAX_BACKFILL_GAP_DAYS_1H}
        runner = self._run(tmp_path, monkeypatch, {"model0": [req]})
        runner.validate_data_prerequisites()  # no raise

    def test_cache_only_passes(self, tmp_path, monkeypatch):
        cache = _write_bars_parquet(tmp_path / "cache.parquet", FRESH,
                                    as_column=False)
        req = {"stream": "1h", "cache": cache,
               "seed": tmp_path / "absent_seed.parquet",
               "max_gap_days": MAX_BACKFILL_GAP_DAYS_1H}
        runner = self._run(tmp_path, monkeypatch, {"model0": [req]})
        runner.validate_data_prerequisites()

    def test_corrupted_cache_raises_with_delete_remedy(self, tmp_path, monkeypatch):
        cache = tmp_path / "cache.parquet"
        cache.write_bytes(b"this is not a parquet file")
        seed = _write_bars_parquet(tmp_path / "seed.parquet", FRESH)
        req = {"stream": "1h", "cache": cache, "seed": seed,
               "max_gap_days": MAX_BACKFILL_GAP_DAYS_1H}
        runner = self._run(tmp_path, monkeypatch, {"model0": [req]})
        with pytest.raises(RuntimeError, match="Delete it and relaunch"):
            runner.validate_data_prerequisites()

    def test_stale_seed_raises_with_databento_remedy(self, tmp_path, monkeypatch):
        seed = _write_bars_parquet(tmp_path / "seed.parquet", STALE_1H)
        req = {"stream": "1h", "cache": tmp_path / "absent.parquet",
               "seed": seed, "max_gap_days": MAX_BACKFILL_GAP_DAYS_1H}
        runner = self._run(tmp_path, monkeypatch, {"model0": [req]})
        with pytest.raises(RuntimeError, match="Databento"):
            runner.validate_data_prerequisites()

    def test_fresh_cache_rescues_stale_seed(self, tmp_path, monkeypatch):
        seed = _write_bars_parquet(tmp_path / "seed.parquet", STALE_1H)
        cache = _write_bars_parquet(tmp_path / "cache.parquet", FRESH,
                                    as_column=False)
        req = {"stream": "1h", "cache": cache, "seed": seed,
               "max_gap_days": MAX_BACKFILL_GAP_DAYS_1H}
        runner = self._run(tmp_path, monkeypatch, {"model0": [req]})
        runner.validate_data_prerequisites()

    def test_extra_args_seed_path_override_honored(self, tmp_path, monkeypatch):
        real_seed = _write_bars_parquet(tmp_path / "override_seed.parquet", FRESH)
        req = {"stream": "5m", "cache": tmp_path / "absent.parquet",
               "seed": tmp_path / "default_absent.parquet",
               "max_gap_days": MAX_BACKFILL_GAP_DAYS_5M}
        runner = self._run(tmp_path, monkeypatch, {"model0": [req]},
                           extra_args=["--seed-path", str(real_seed)])
        runner.validate_data_prerequisites()  # passes via the override

    def test_macro_model_missing_fred_key_raises(self, tmp_path, monkeypatch):
        monkeypatch.delenv("FRED_API_KEY", raising=False)
        seed = _write_bars_parquet(tmp_path / "seed.parquet", FRESH)
        req = {"stream": "1h", "cache": tmp_path / "absent.parquet",
               "seed": seed, "max_gap_days": MAX_BACKFILL_GAP_DAYS_1H}
        runner = self._run(
            tmp_path, monkeypatch, {"model0": [req]},
            features={"model0": ["RSI_14", "MACRO_VIX_ZS_1W"]},
        )
        # Fake strategy config lacks a resolvable instrument -> the macro
        # branch fails loudly (resolve error surfaces as a problem line).
        with pytest.raises(RuntimeError, match="model0"):
            runner.validate_data_prerequisites()

    def test_multi_instance_aggregates_all_problems(self, tmp_path, monkeypatch):
        bad = {"stream": "1h", "cache": tmp_path / "nc.parquet",
               "seed": tmp_path / "ns.parquet",
               "max_gap_days": MAX_BACKFILL_GAP_DAYS_1H}
        runner = self._run(tmp_path, monkeypatch,
                           {"model0": [bad], "model1": [bad]}, n_configs=2)
        with pytest.raises(RuntimeError) as exc:
            runner.validate_data_prerequisites()
        assert "model0" in str(exc.value) and "model1" in str(exc.value)

    def test_preflight_requires_validate_first(self, tmp_path):
        manifest = _write_manifest(
            tmp_path, [_write_config(tmp_path, "m", 1000)])
        runner = FleetRunner(manifest_path=manifest,
                             popen=lambda *a, **k: None, sleep=lambda s: None)
        runner.load_manifest()
        with pytest.raises(ValueError, match="before validate"):
            runner.validate_data_prerequisites()
