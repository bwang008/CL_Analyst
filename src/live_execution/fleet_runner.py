"""
Fleet Runner — multi-strategy supervisor for the live execution CLI.

Launches and monitors one `src.live_execution.cli` subprocess per enabled
instance in a fleet manifest (configs/fleet/fleet_manifest.json). This is the
process-per-config supervisor described in ticket
multi-config-fleet-runner_07042026_0646: systemd restarts the *runner*; the
runner restarts crashed *children*.

Manifest schema (ALL top-level fields required — project rule: no silent
null defaults; missing fields RAISE instead of defaulting):

    {
      "instances": [
        {
          "config": "configs/strategies/HS14B_Sharpe_E01_06262026.json",
          "enabled": true,
          "extra_args": []
        }
      ],
      "stagger_seconds": 60,
      "data_port": 4002,
      "exec_port": 4002
    }

IBKR budget notes (see blueprint):
- Each instance consumes TWO client ids: cid (data feed) and cid+1
  (execution) — cli.py connects at client_id and client_id + 1. Hence
  client_ids must be unique AND spaced >= 2 apart.
- 32 simultaneous API connections per gateway → 2 * N <= 32 → N <= 16
  enabled instances on the single-gateway deployment.
- Startup warm-start backfill can trip historical pacing if instances start
  simultaneously → the runner sleeps `stagger_seconds` between launches.

Error event queue (fleet_error_events.py): when an error_writer is injected
(main() does this by default; --no-error-queue opts out), each child's
stderr is redirected to reports/fleet_stderr/<name>.stderr.log and every
crash detected by poll_once() emits a structured JSON event into
.agents/collab/error_queue/pending/ for the AI triage pipeline
(.agents/skills/fleet-error-monitor/SKILL.md). With error_writer=None the
runner behaves exactly as before (children inherit stdout/stderr).

Usage:
    python -m src.live_execution.fleet_runner \
        --manifest configs/fleet/fleet_manifest.json
"""

import argparse
import json
import logging
import signal
import subprocess
import sys
import time
from pathlib import Path

log = logging.getLogger("FleetRunner")

# Hard IBKR limit: 32 simultaneous API client connections per gateway.
MAX_CONNECTIONS_PER_GATEWAY = 32
CONNECTIONS_PER_INSTANCE = 2  # data feed (cid) + execution (cid + 1)
MAX_ENABLED_INSTANCES = MAX_CONNECTIONS_PER_GATEWAY // CONNECTIONS_PER_INSTANCE

# Restart backoff: base * 2^(n-1), capped.
RESTART_BACKOFF_BASE_SECONDS = 5.0
RESTART_BACKOFF_CAP_SECONDS = 300.0

DEFAULT_MAX_RESTARTS = 5
DEFAULT_POLL_INTERVAL_SECONDS = 10.0

REQUIRED_MANIFEST_KEYS = ("instances", "stagger_seconds", "data_port", "exec_port")
REQUIRED_INSTANCE_KEYS = ("config", "enabled")


class _Instance:
    """Bookkeeping for one enabled fleet instance and its child process."""

    def __init__(self, config_path, extra_args, client_id):
        self.config_path = config_path
        self.extra_args = list(extra_args)
        self.client_id = client_id
        self.cmd = None          # built at launch time
        self.proc = None         # live Popen handle (None = not running)
        self.restarts = 0        # restarts consumed so far
        self.gave_up = False     # restart cap exhausted
        self.stderr_sink = None  # open file handle when error queue is on

    @property
    def name(self):
        return Path(self.config_path).stem


class FleetRunner:
    """Supervisor that launches/monitors one live-CLI subprocess per config.

    `popen` and `sleep` are dependency-injected so tests never spawn real
    processes or block; defaults are `subprocess.Popen` and `time.sleep`.
    """

    def __init__(self, manifest_path, popen=subprocess.Popen, sleep=time.sleep,
                 max_restarts=DEFAULT_MAX_RESTARTS, error_writer=None):
        self.manifest_path = Path(manifest_path)
        self._popen = popen
        self._sleep = sleep
        self.max_restarts = int(max_restarts)
        # Optional FleetErrorEventWriter (fleet_error_events.py). None keeps
        # the pre-error-queue behavior byte-identical (Strict-Locked tests).
        self._error_writer = error_writer

        self.manifest = None          # raw parsed manifest dict
        self.instances = []           # _Instance for each ENABLED entry
        self._validated = False
        self._shutting_down = False

    # ------------------------------------------------------------------
    # Manifest loading
    # ------------------------------------------------------------------
    def load_manifest(self):
        """Parse the fleet manifest. RAISES on any missing required field
        (project rule: no silent null defaults)."""
        if not self.manifest_path.exists():
            raise FileNotFoundError(
                f"Fleet manifest not found: {self.manifest_path}"
            )
        with open(self.manifest_path, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)

        missing = [k for k in REQUIRED_MANIFEST_KEYS if k not in manifest]
        if missing:
            raise ValueError(
                f"Fleet manifest {self.manifest_path} is missing required "
                f"field(s) {missing}. All of {list(REQUIRED_MANIFEST_KEYS)} "
                f"must be explicit (no silent defaults)."
            )

        instances = manifest["instances"]
        if not isinstance(instances, list) or not instances:
            raise ValueError(
                f"Fleet manifest {self.manifest_path}: 'instances' must be a "
                f"non-empty list, got {instances!r}."
            )

        for i, entry in enumerate(instances):
            entry_missing = [k for k in REQUIRED_INSTANCE_KEYS if k not in entry]
            if entry_missing:
                raise ValueError(
                    f"Fleet manifest instance #{i} is missing required "
                    f"field(s) {entry_missing}: {entry!r}"
                )

        self.manifest = manifest
        self._validated = False
        log.info(
            "Loaded fleet manifest %s: %d instance(s) (%d enabled), "
            "stagger=%ss, data_port=%s, exec_port=%s",
            self.manifest_path, len(instances),
            sum(1 for e in instances if e["enabled"]),
            manifest["stagger_seconds"], manifest["data_port"],
            manifest["exec_port"],
        )
        return manifest

    # ------------------------------------------------------------------
    # Pre-launch validation (fail fast — nothing is spawned here)
    # ------------------------------------------------------------------
    def validate(self):
        """Validate configs, client ids, and gateway capacity BEFORE any
        launch. Raises ValueError / FileNotFoundError on violation."""
        if self.manifest is None:
            raise ValueError("validate() called before load_manifest().")

        # (a) every referenced strategy config path must exist.
        for entry in self.manifest["instances"]:
            cfg_path = Path(entry["config"])
            if not cfg_path.exists():
                raise FileNotFoundError(
                    f"Strategy config referenced by fleet manifest does not "
                    f"exist: {cfg_path}"
                )

        enabled = [e for e in self.manifest["instances"] if e["enabled"]]

        # (b) capacity: each instance consumes 2 connections (cid, cid+1);
        #     a single gateway allows 32 simultaneous API connections.
        if len(enabled) > MAX_ENABLED_INSTANCES:
            raise ValueError(
                f"{len(enabled)} enabled instances would require "
                f"{CONNECTIONS_PER_INSTANCE * len(enabled)} IBKR connections, "
                f"exceeding the {MAX_CONNECTIONS_PER_GATEWAY}-connection limit "
                f"of a single gateway (max {MAX_ENABLED_INSTANCES} instances)."
            )

        # (c) explicit live_config.client_id in every enabled config;
        #     unique AND spaced >= 2 apart (each instance also uses cid+1).
        instances = []
        for entry in enabled:
            cfg_path = Path(entry["config"])
            with open(cfg_path, "r", encoding="utf-8") as fh:
                strategy_cfg = json.load(fh)
            live_cfg = strategy_cfg.get("live_config")
            if not isinstance(live_cfg, dict) or "client_id" not in live_cfg:
                raise ValueError(
                    f"Strategy config {cfg_path} has no explicit "
                    f"live_config.client_id — refusing to launch (no silent "
                    f"default client ids; Error-326 auto-increment would mask "
                    f"the collision)."
                )
            client_id = live_cfg["client_id"]
            instances.append(
                _Instance(cfg_path, entry.get("extra_args", []), client_id)
            )

        by_cid = sorted(instances, key=lambda inst: inst.client_id)
        for a, b in zip(by_cid, by_cid[1:]):
            if b.client_id - a.client_id < CONNECTIONS_PER_INSTANCE:
                raise ValueError(
                    f"client_id collision: {a.config_path} (cid {a.client_id}, "
                    f"also consumes {a.client_id + 1} for execution) vs "
                    f"{b.config_path} (cid {b.client_id}). client_ids must be "
                    f"unique and spaced >= {CONNECTIONS_PER_INSTANCE} apart."
                )

        self.instances = instances
        self._validated = True
        log.info(
            "Fleet manifest validated: %d enabled instance(s), client_ids=%s",
            len(instances), [inst.client_id for inst in instances],
        )

    # ------------------------------------------------------------------
    # Pre-launch DATA preflight (ticket fleet-seed-preflight-gap_07052026_2215)
    # ------------------------------------------------------------------
    # Deliberately a SEPARATE step from validate(): the Strict-Locked
    # supervision tests exercise validate() on minimal symbol-less fixtures
    # and must stay untouched. main() runs both before launch_all().
    # Function-local imports keep this module stdlib-only at import time.

    def _load_strategy(self, config_path):
        """Load the live strategy exactly as cli.py does (models included).

        Returns (strategy_config_dict, feature_names). Overridable in tests.
        """
        from src.live_execution.strategies.configurable_strategy import (
            ConfigurableStrategy,
        )
        strategy = ConfigurableStrategy(config_path=str(config_path))
        return strategy.config, list(strategy.feature_names)

    @staticmethod
    def _newest_bar_ts(path):
        """Newest bar timestamp in a seed/cache artifact. Raises if unreadable.

        Accepts the two on-disk layouts: DatetimeIndex parquet (warm-start
        caches) and DateTime-column parquet (<SYM>_raw_1h seeds). CSV seeds
        (5m ``<sym>-5m_bk.csv``) are timestamped in col 0/1 semicolon format;
        for preflight purposes a cheap tail parse suffices.
        """
        import pandas as pd
        p = Path(path)
        if p.suffix == ".parquet":
            df = pd.read_parquet(p)
            if "DateTime" in df.columns:
                return pd.to_datetime(df["DateTime"]).max()
            if isinstance(df.index, pd.DatetimeIndex):
                return df.index.max()
            raise ValueError(
                f"{p} has neither a DatetimeIndex nor a DateTime column"
            )
        # CSV seed: semicolon format DD/MM/YYYY;HH:MM;O;H;L;C;V
        df = pd.read_csv(p, sep=";", header=None, usecols=[0, 1],
                         names=["d", "t"], dtype=str)
        last = df.iloc[-1]
        return pd.to_datetime(f"{last['d']} {last['t']}", dayfirst=True)

    def _check_requirement(self, name, req, now):
        """Evaluate one stream requirement. Returns a list of problem strings."""
        problems = []
        cache, seed = req["cache"], req["seed"]
        cache_ts = seed_ts = None

        if cache.exists():
            try:
                cache_ts = self._newest_bar_ts(cache)
            except Exception as e:
                # User ruling 2026-07-05: corrupted caches must be DELETED to
                # rebuild from seed — tell the operator exactly that.
                problems.append(
                    f"{name}: warm-start cache is unreadable/corrupted "
                    f"({type(e).__name__}: {e}). Delete it and relaunch — the "
                    f"seed will rebuild it:\n"
                    f"    Remove-Item \"{cache}\""
                )
                return problems

        if seed.exists():
            try:
                seed_ts = self._newest_bar_ts(seed)
            except Exception as e:
                if cache_ts is None:
                    problems.append(
                        f"{name}: seed file is unreadable ({type(e).__name__}: "
                        f"{e}): {seed}"
                    )
                    return problems
                log.warning(
                    "%s: seed %s unreadable but cache is healthy — child will "
                    "run from cache. Re-stage the seed before the next cache "
                    "rebuild.", name, seed,
                )

        if cache_ts is None and seed_ts is None:
            problems.append(
                f"{name}: neither {req['stream']} cache nor seed found "
                f"(the child would crash-loop exactly like NG/GC on "
                f"2026-07-05).\n"
                f"    cache: {cache}\n"
                f"    seed:  {seed}\n"
                f"  Remedy (1h): Copy-Item <SYM>_raw.parquet <SYM>_raw_1h.parquet "
                f"(already hourly); (5m): stage the Databento 5m seed."
            )
            return problems

        freshest = max(ts for ts in (cache_ts, seed_ts) if ts is not None)
        gap_days = (now - freshest).total_seconds() / 86_400.0
        if gap_days > req["max_gap_days"]:
            problems.append(
                f"{name}: newest {req['stream']} bar is {gap_days:.0f} days old "
                f"({freshest}) — beyond the ~{req['max_gap_days']}-day IBKR "
                f"backfill horizon; the gap is UNFILLABLE at startup and would "
                f"leave a silent hole in the series. Refresh the data via the "
                f"Databento download (/grab-data workflow) and re-stage "
                f"{req['seed'].name}."
            )
        return problems

    def validate_data_prerequisites(self):
        """Per-instance data preflight — raises BEFORE any child is spawned.

        Mirrors the artifacts LiveTrader hard-requires (via the shared
        authority ``required_live_data_artifacts``): seed/cache presence,
        cache readability (corruption -> actionable delete message),
        staleness vs the IBKR backfill horizon, macro CSVs + FRED_API_KEY
        for macro-dependent models. Honors ``--seed-path``/``--cache-path``
        in an instance's ``extra_args`` (cli.py overrides for the 5m stream).
        """
        if not self._validated:
            raise ValueError(
                "validate_data_prerequisites() called before validate()."
            )
        # src.data_paths loads .env at import — CL_DATA_ROOT / FRED_API_KEY
        # resolve here exactly as the children will see them.
        import os

        import pandas as pd

        from src.data_paths import get_data_path  # noqa: F401 (env load side effect + macro paths)
        from src.live_execution.data_manager import required_live_data_artifacts
        from src.features.macro_features import (
            has_external_macro_features,
            validate_external_macro_features,
        )
        from src.live_execution.instrument_context import (
            resolve_instrument_context,
        )

        now = pd.Timestamp.now().tz_localize(None)
        problems = []
        for inst in self.instances:
            name = inst.name
            try:
                cfg, feature_names = self._load_strategy(inst.config_path)
                reqs = required_live_data_artifacts(cfg)

                # cli.py --seed-path/--cache-path override the 5m artifacts;
                # honor them so we never check the wrong files (reviewer
                # nuance B).
                overrides = dict(zip(inst.extra_args, inst.extra_args[1:]))
                for req in reqs:
                    if req["stream"] == "5m":
                        if "--seed-path" in overrides:
                            req["seed"] = Path(overrides["--seed-path"])
                        if "--cache-path" in overrides:
                            req["cache"] = Path(overrides["--cache-path"])

                for req in reqs:
                    problems.extend(self._check_requirement(name, req, now))

                if has_external_macro_features(feature_names):
                    ctx = resolve_instrument_context(cfg)
                    validate_external_macro_features(
                        feature_names, ctx.brain_instrument
                    )
                    sym_l = ctx.brain_symbol.lower()
                    # Deliberate policy (user-approved 2026-07-05): a live
                    # launch must not depend on a T0 network fetch — the
                    # macro CSVs must be pre-staged and the FRED key present
                    # (a stale CSV without the key is a deterministic
                    # startup ValueError).
                    for csv_rel in (
                        f"raw/macro/fred_macro_data_{sym_l}.csv",
                        f"raw/macro/cftc_cot_{sym_l}.csv",
                    ):
                        csv_path = Path(get_data_path(csv_rel))
                        if not csv_path.exists():
                            problems.append(
                                f"{name}: macro artifact missing: {csv_path} — "
                                f"run: conda run -n trader python "
                                f"scripts/download_macro_data.py --symbol "
                                f"{ctx.brain_symbol}"
                            )
                    if not os.environ.get("FRED_API_KEY"):
                        problems.append(
                            f"{name}: FRED_API_KEY is not set — the macro "
                            f"refresh raises at startup whenever the FRED CSV "
                            f"predates the last 7:00 PM ET cutoff. Add it to "
                            f".env."
                        )
            except Exception as e:  # config/resolve/model-load failures
                problems.append(f"{name}: {type(e).__name__}: {e}")

        if problems:
            raise RuntimeError(
                "Fleet data preflight FAILED — NOTHING was launched. Fix the "
                "items below (or park the instance with \"enabled\": false to "
                "launch the rest):\n  - " + "\n  - ".join(problems)
            )
        log.info(
            "Fleet data preflight passed: %d instance(s) have their seeds/"
            "caches/macro artifacts staged and fresh.", len(self.instances),
        )

    # ------------------------------------------------------------------
    # Launch
    # ------------------------------------------------------------------
    def _build_command(self, instance):
        return [
            sys.executable, "-m", "src.live_execution.cli",
            "--config", str(instance.config_path),
            "--data-port", str(self.manifest["data_port"]),
            "--exec-port", str(self.manifest["exec_port"]),
        ] + list(instance.extra_args)

    def _spawn(self, instance):
        instance.cmd = self._build_command(instance)
        if self._error_writer is None:
            # Children inherit stdout/stderr (→ journald under systemd); they
            # also keep their own per-cid file logging via _setup_file_logging.
            instance.proc = self._popen(instance.cmd)
        else:
            # Error queue on: child stderr goes to a per-instance sink FILE
            # (never PIPE — an unread pipe fills and deadlocks the child) so
            # poll_once() can read the traceback after a crash.
            self._close_stderr_sink(instance)
            instance.stderr_sink = self._error_writer.open_stderr_sink(
                instance.name
            )
            instance.proc = self._popen(instance.cmd,
                                        stderr=instance.stderr_sink)
        log.info(
            "Launched %s (client_id=%s, pid=%s): %s",
            instance.name, instance.client_id,
            getattr(instance.proc, "pid", "?"), " ".join(instance.cmd),
        )

    @staticmethod
    def _close_stderr_sink(instance):
        if instance.stderr_sink is not None:
            try:
                instance.stderr_sink.close()
            except OSError:
                pass
            instance.stderr_sink = None

    def launch_all(self):
        """Spawn one child per enabled instance, sleeping stagger_seconds
        between consecutive launches (historical-pacing protection during
        warm-start backfill)."""
        if not self._validated:
            raise ValueError("launch_all() called before validate().")
        stagger = self.manifest["stagger_seconds"]
        for i, instance in enumerate(self.instances):
            if i > 0:
                log.info("Staggering %ss before next launch...", stagger)
                self._sleep(stagger)
            self._spawn(instance)

    # ------------------------------------------------------------------
    # Supervision
    # ------------------------------------------------------------------
    def poll_once(self):
        """One supervision pass: restart dead children with capped
        exponential backoff. Returns the number of live children."""
        live = 0
        for instance in self.instances:
            if self._shutting_down or instance.gave_up or instance.proc is None:
                continue
            exit_code = instance.proc.poll()
            if exit_code is None:
                live += 1
                continue

            log.warning(
                "Child %s (client_id=%s) exited with code %s "
                "(restarts used: %d/%d)",
                instance.name, instance.client_id, exit_code,
                instance.restarts, self.max_restarts,
            )
            will_give_up = instance.restarts >= self.max_restarts
            if self._error_writer is not None:
                # Queue the structured crash event for the AI triage
                # pipeline BEFORE the backoff/give-up path. Never raises.
                self._error_writer.emit_crash_event(
                    instance, exit_code, gave_up=will_give_up,
                )
            if will_give_up:
                instance.gave_up = True
                instance.proc = None
                self._close_stderr_sink(instance)
                log.error(
                    "Child %s exhausted its restart cap (%d) — NOT "
                    "restarting. Manual intervention required.",
                    instance.name, self.max_restarts,
                )
                continue

            instance.restarts += 1
            backoff = min(
                RESTART_BACKOFF_BASE_SECONDS * 2 ** (instance.restarts - 1),
                RESTART_BACKOFF_CAP_SECONDS,
            )
            log.info(
                "Restarting %s in %.0fs (restart %d/%d)...",
                instance.name, backoff, instance.restarts, self.max_restarts,
            )
            self._sleep(backoff)
            self._spawn(instance)
            live += 1
        return live

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------
    def shutdown(self):
        """Terminate every live child. Never spawns or restarts anything."""
        self._shutting_down = True
        for instance in self.instances:
            proc = instance.proc
            if proc is None:
                continue
            if proc.poll() is None:
                log.info(
                    "Terminating %s (client_id=%s, pid=%s)...",
                    instance.name, instance.client_id,
                    getattr(proc, "pid", "?"),
                )
                proc.terminate()
        # Wait for graceful exits (cache save + IBKR disconnect) before
        # systemd's TimeoutStopSec would SIGKILL us.
        for instance in self.instances:
            proc = instance.proc
            if proc is None:
                continue
            try:
                proc.wait(timeout=60)
            except Exception:
                log.error(
                    "Child %s did not exit within 60s — killing.",
                    instance.name,
                )
                proc.kill()
        for instance in self.instances:
            self._close_stderr_sink(instance)
        log.info("Fleet shutdown complete.")


# ----------------------------------------------------------------------
# CLI entry point
# ----------------------------------------------------------------------
def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Fleet Runner — launch and supervise one live-execution "
                    "CLI process per enabled instance in a fleet manifest.",
    )
    parser.add_argument(
        "--manifest", required=True,
        help="Path to the fleet manifest JSON "
             "(e.g. configs/fleet/fleet_manifest.json)",
    )
    parser.add_argument(
        "--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL_SECONDS,
        help="Seconds between supervision passes "
             f"(default: {DEFAULT_POLL_INTERVAL_SECONDS})",
    )
    parser.add_argument(
        "--max-restarts", type=int, default=DEFAULT_MAX_RESTARTS,
        help=f"Per-instance restart cap (default: {DEFAULT_MAX_RESTARTS})",
    )
    parser.add_argument(
        "--no-error-queue", action="store_true",
        help="Disable the crash → error-event queue (children then inherit "
             "stdout/stderr instead of logging stderr to per-instance files)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    error_writer = None
    if not args.no_error_queue:
        # Function-local imports keep this module stdlib-only at import time.
        from src.live_execution.fleet_error_events import (
            DEFAULT_INFRA_PATTERNS_PATH,
            DEFAULT_QUEUE_DIR,
            DEFAULT_STDERR_DIR,
            FleetErrorEventWriter,
        )
        from src.live_execution.utils.telegram_alert import TelegramAlerter
        # Raises here (before any spawn) if infra_patterns.json is missing —
        # no silent null defaults.
        error_writer = FleetErrorEventWriter(
            queue_dir=DEFAULT_QUEUE_DIR,
            stderr_dir=DEFAULT_STDERR_DIR,
            manifest_path=args.manifest,
            patterns_path=DEFAULT_INFRA_PATTERNS_PATH,
            telegram=TelegramAlerter(prefix="FLEET"),
        )

    runner = FleetRunner(manifest_path=args.manifest,
                         max_restarts=args.max_restarts,
                         error_writer=error_writer)
    runner.load_manifest()
    runner.validate()
    # Data preflight AFTER structural validation, BEFORE any spawn — one
    # unstaged symbol blocks the WHOLE launch (user-confirmed all-or-nothing;
    # park an instance with "enabled": false to launch the rest).
    runner.validate_data_prerequisites()

    def _handle_signal(signum, _frame):
        log.info("Received signal %s — shutting down fleet.", signum)
        runner.shutdown()
        raise SystemExit(0)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    runner.launch_all()
    try:
        while True:
            live = runner.poll_once()
            if live == 0:
                log.error(
                    "All children are dead and restart caps are exhausted — "
                    "exiting nonzero so systemd restarts the runner."
                )
                runner.shutdown()
                return 1
            time.sleep(args.poll_interval)
    except KeyboardInterrupt:
        runner.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
