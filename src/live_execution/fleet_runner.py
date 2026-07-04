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

    @property
    def name(self):
        return Path(self.config_path).stem


class FleetRunner:
    """Supervisor that launches/monitors one live-CLI subprocess per config.

    `popen` and `sleep` are dependency-injected so tests never spawn real
    processes or block; defaults are `subprocess.Popen` and `time.sleep`.
    """

    def __init__(self, manifest_path, popen=subprocess.Popen, sleep=time.sleep,
                 max_restarts=DEFAULT_MAX_RESTARTS):
        self.manifest_path = Path(manifest_path)
        self._popen = popen
        self._sleep = sleep
        self.max_restarts = int(max_restarts)

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
        # Children inherit stdout/stderr (→ journald under systemd); they also
        # keep their own per-cid file logging via _setup_file_logging.
        instance.proc = self._popen(instance.cmd)
        log.info(
            "Launched %s (client_id=%s, pid=%s): %s",
            instance.name, instance.client_id,
            getattr(instance.proc, "pid", "?"), " ".join(instance.cmd),
        )

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
            if instance.restarts >= self.max_restarts:
                instance.gave_up = True
                instance.proc = None
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
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    runner = FleetRunner(manifest_path=args.manifest,
                         max_restarts=args.max_restarts)
    runner.load_manifest()
    runner.validate()

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
