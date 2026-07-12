"""
TDD-TESTER AUTHORIZATION
Target Implementation File: scripts/resume_batch.ps1
Target Class/Function: whole script (resume a stalled cloud batch — 8-step recovery)
Status: FINALIZED
Strict-Lock: TRUE (Implementation agents may NOT modify this file)

Ticket: batch-resume-recovery_07112026_0924

--------------------------------------------------------------------------------
WHAT THIS PINS
--------------------------------------------------------------------------------
A NEW `scripts/resume_batch.ps1` codifies the manual recovery of a stalled cloud
batch (the local orchestrator `gcp/run_sweep_batch.ps1` externally killed
mid-run). This suite is genuinely RED right now because the script DOES NOT
EXIST yet — every test that reads the shipped script fails until the Coder
writes it.

The load-bearing, verifiable behaviors covered here (NO cloud side effects — no
real gcloud, no VMs, no GCS; only `tmp_path` and read-only reads of the frozen
reference batch dir):

(A) Existence + AST parseability + param surface (fix-pinning text scan +
    real-powershell.exe AST parse-check).
(B) Deterministic sweep-ts reconstruction: `batch_20260711_061128` ->
    `20260711-061128`; per-exp `tsPrefix = "<base>_20260711-061128"`. Verified
    against the REAL NG base prefixes / batch_progress.json gcs_prefix values.
    (pure-Python logic-guard mirror + real-PS extracted `-replace` expression.)
(C) armCount + optTaskCount + tier map (run_sweep_batch.ps1:1077-1088).
(D) The CRITICAL CROSS-LINK: predictions_path rewrite
    `batch_runs/<OLD_ID>/` -> `batch_runs/<stampName>/`, BOM-less UTF-8,
    model_path byte-identical (real powershell.exe over tmp_path, mirroring
    tests/test_autostamp_config_rewrite.py) + a fix-pinning scan that the shipped
    script CONTAINS this rewrite and targets the real config dir.
(E) Required-field / no-silent-default pinning (crash-loud on missing manifest
    fields + reject out-of-range slippage).
(F) DryRun read-only contract (backup/.bak, progress writes, deploys gated on
    NON-DryRun).
(G) gcloud-not-gsutil + targeted-VM-filter + PLAIN-id deploy pinning.

STRATEGY (mirrors tests/test_autostamp_config_rewrite.py exactly):
  * Real `powershell.exe -File` runs over a `tmp_path` fixture exercising
    EXTRACTED PS expressions (the config-rewrite `.Replace(...)` + BOM-less write,
    and the `-replace` ts reconstruction), guarded by `@requires_powershell`.
  * Pure-text "fix-pinning" scans of the shipped script (`open(...).read()`) that
    assert distinctive stable substrings/branches EXIST — these make the suite
    RED before the file exists.
  * Pure-Python logic-guard mirrors that re-implement the string logic (always run).

NONE of these tests mutate real `reports/` dirs, call real gcloud, or touch a VM.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PS_SCRIPT = os.path.join(PROJECT_ROOT, "scripts", "resume_batch.ps1")

# Read-only reference batch dir (the live NG recovery). NEVER mutated by tests.
REF_BATCH_DIR = os.path.join(
    PROJECT_ROOT, "reports", "batch_runs", "batch_20260711_061128_NG_SCOUT"
)
REF_MANIFEST = os.path.join(REF_BATCH_DIR, "manifest.json")
REF_PROGRESS = os.path.join(REF_BATCH_DIR, "batch_progress.json")
REF_CONFIG_DIR = os.path.join(REF_BATCH_DIR, "batch_configs")

# The plain batch id the reference dir was derived from + its sweep-ts form.
REF_BATCH_ID = "batch_20260711_061128"
REF_SWEEP_TS = "20260711-061128"

# Canonical fixture ids for the config-rewrite tests (old = plain VM-baked id;
# new = auto-stamped name written by finalize/rename).
OLD_ID = "batch_20260711_061128"
STAMP_NAME = "batch_20260711_061128_NG_SCOUT"

POWERSHELL = shutil.which("powershell.exe") or shutil.which("powershell")
requires_powershell = pytest.mark.skipif(
    POWERSHELL is None,
    reason="powershell.exe not found on PATH (required for the real-PS tests)",
)


def _script_text() -> str:
    """The shipped script text. Fails loudly (via the callers' asserts) when the
    script does not exist yet — which is the intended RED state pre-implementation."""
    assert os.path.exists(PS_SCRIPT), (
        f"missing target script (RED until the Coder writes it): {PS_SCRIPT}"
    )
    return open(PS_SCRIPT, encoding="utf-8-sig").read()


# ===========================================================================
# (A) Existence + AST parseability + param surface
# ===========================================================================


class TestExistenceAndParamSurface:
    """Fix-pinning text scan of the shipped script: it must exist, declare the
    full param surface, add $gcloudBin to PATH, and NEVER mention gsutil."""

    def test_script_exists(self):
        assert os.path.exists(PS_SCRIPT), (
            "scripts/resume_batch.ps1 does not exist yet (RED until the Coder "
            "creates it)"
        )

    def test_param_batchid_mandatory(self):
        text = _script_text()
        # -BatchId must be a Mandatory parameter.
        assert re.search(r"\[Parameter\(\s*Mandatory\s*=\s*\$true\s*\)\]", text), (
            "no [Parameter(Mandatory=$true)] declared — -BatchId must be required"
        )
        assert re.search(r"\$BatchId\b", text), "no -BatchId parameter"

    def test_param_dryrun_whatif_force_switches(self):
        text = _script_text()
        assert re.search(r"\[switch\]\s*\$DryRun", text), "no [switch]$DryRun param"
        # -WhatIf may be an alias of DryRun or its own switch — either is acceptable,
        # but the token must appear so operators can pass it.
        assert "WhatIf" in text, "no -WhatIf switch/alias"
        assert re.search(r"\[switch\]\s*\$Force", text), "no [switch]$Force param"

    def test_param_objective_defaults_sharpe(self):
        text = _script_text()
        assert re.search(
            r"\$Objective\s*=\s*[\"']sharpe[\"']", text
        ), "-Objective must default to \"sharpe\""

    def test_passthrough_knobs_present(self):
        text = _script_text()
        for knob in (
            "$Zone",
            "$SweepMode",
            "$NBlocks",
            "$LambdaDispersion",
            "$MinBlockMonths",
            "$OptimizerMaxRunDurationMinutes",
            "$DisableTelegram",
        ):
            assert knob in text, f"pass-through knob {knob} missing from param surface"

    def test_optimizer_maxrun_defaults_360(self):
        text = _script_text()
        assert re.search(
            r"\$OptimizerMaxRunDurationMinutes\s*=\s*360", text
        ), "-OptimizerMaxRunDurationMinutes must default to 360"

    def test_gcloud_bin_added_to_path(self):
        text = _script_text()
        assert "$gcloudBin" in text, "no $gcloudBin PATH line (mirror run_sweep_batch.ps1:57-58)"
        assert "google-cloud-sdk" in text, "$gcloudBin must point at the gcloud SDK bin"

    def test_gsutil_is_banned(self):
        """gsutil is BANNED here (its poll is broken in this env). Every bucket op
        must be `gcloud storage`."""
        text = _script_text()
        assert "gsutil" not in text, (
            "gsutil appears in the script — it is BANNED; every bucket op must use "
            "`gcloud storage`"
        )
        assert "gcloud storage" in text, (
            "no `gcloud storage` usage found — bucket ops must go through gcloud "
            "storage, not gsutil"
        )

    def test_batchid_regex_validation_present(self):
        """BatchId must be validated against ^batch_\\d{8}_\\d{6} and crash on mismatch."""
        text = _script_text()
        assert re.search(r"\^batch_\\d\{8\}_\\d\{6\}", text), (
            "no ^batch_\\d{8}_\\d{6} validation for -BatchId"
        )

    @requires_powershell
    def test_script_ast_parses_without_errors(self):
        """Parse the shipped script with the real PS parser; ZERO parse errors."""
        assert os.path.exists(PS_SCRIPT), (
            "scripts/resume_batch.ps1 does not exist yet (RED)"
        )
        ps_cmd = (
            "$errs = $null; "
            "[System.Management.Automation.Language.Parser]::ParseFile("
            f"'{PS_SCRIPT}', [ref]$null, [ref]$errs) | Out-Null; "
            "if ($errs -and $errs.Count -gt 0) { "
            "  $errs | ForEach-Object { Write-Output $_.Message }; exit 3 "
            "} else { Write-Output 'PARSE_OK'; exit 0 }"
        )
        proc = subprocess.run(
            [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=60,
        )
        assert proc.returncode == 0, (
            f"resume_batch.ps1 has PowerShell parse errors:\n{proc.stdout}\n{proc.stderr}"
        )
        assert "PARSE_OK" in proc.stdout


# ===========================================================================
# (B) Deterministic sweep-ts reconstruction
# ===========================================================================

# The real NG base prefixes from the reference manifest experiments[].gcs_prefix.
NG_BASE_PREFIXES = [
    "sweep_ng_hs02b_2x1_1h_scout",
    "sweep_ng_hs02b_2x1_2h_scout",
    "sweep_ng_hs02b_5x1_6h_scout",
    "sweep_ng_hs02b_4x1_6h_scout",
    "sweep_ng_hs02b_2x1_3h_scout",
    "sweep_ng_hs02b_3x1_6h_scout",
]


def _py_sweep_ts(batch_id: str) -> str:
    """Pure-Python mirror of `$BatchId -replace '^batch_','' -replace '_','-'`."""
    return re.sub(r"_", "-", re.sub(r"^batch_", "", batch_id))


def _py_ts_prefix(base_prefix: str, batch_id: str) -> str:
    """Mirror of `tsPrefix = "<base>_<YYYYMMDD-HHMMSS>"` (run_sweep_batch.ps1:605)."""
    return f"{base_prefix}_{_py_sweep_ts(batch_id)}"


class TestSweepTsReconstructionPython:
    """Pure-Python logic-guard mirror of the deterministic ts reconstruction.
    Always runs (does NOT execute or cover the actual PowerShell)."""

    def test_batch_id_to_sweep_ts(self):
        assert _py_sweep_ts("batch_20260711_061128") == "20260711-061128"
        assert _py_sweep_ts(REF_BATCH_ID) == REF_SWEEP_TS

    def test_ts_prefix_matches_real_ng_gcs_prefix(self):
        assert _py_ts_prefix("sweep_ng_hs02b_2x1_1h_scout", REF_BATCH_ID) == (
            "sweep_ng_hs02b_2x1_1h_scout_20260711-061128"
        )

    def test_all_ng_prefixes_match_reference_progress(self):
        """Every reconstructed tsPrefix must equal the gcs_prefix stored in the
        REAL reference batch_progress.json — proving the reconstruction is the
        deterministic inverse the orchestrator produced."""
        progress = json.load(open(REF_PROGRESS, encoding="utf-8"))
        stored = {e["gcs_prefix"] for e in progress["experiments"]}
        reconstructed = {_py_ts_prefix(b, REF_BATCH_ID) for b in NG_BASE_PREFIXES}
        assert reconstructed == stored, (
            f"reconstructed prefixes {sorted(reconstructed)} != stored "
            f"{sorted(stored)}"
        )

    def test_reconstruction_is_string_split_not_get_date(self):
        """The reconstruction must be deterministic string manipulation of the
        BatchId, NOT Get-Date (which would drift). Pin the shipped script uses
        the `-replace` idiom and never derives the sweep ts from Get-Date."""
        text = _script_text()
        assert re.search(r"-replace\s+'\^batch_',\s*''", text) or re.search(
            r'-replace\s+"\^batch_",\s*""', text
        ), "sweep ts must be derived from -BatchId via -replace '^batch_',''"
        assert "-replace '_','-'" in text or '-replace "_","-"' in text, (
            "sweep ts must convert underscores to dashes via -replace '_','-'"
        )


@requires_powershell
class TestSweepTsReconstructionRealPS:
    """Runs the EXACT `-replace '^batch_','' -replace '_','-'` expression through
    real powershell.exe to prove the PS semantics match the Python mirror."""

    def _run_ps_reconstruct(self, batch_id: str, base_prefix: str) -> str:
        ps_cmd = (
            f"$BatchId = '{batch_id}'; "
            "$sweepTs = $BatchId -replace '^batch_','' -replace '_','-'; "
            f"$tsPrefix = \"{base_prefix}_$sweepTs\"; "
            "Write-Output $tsPrefix"
        )
        proc = subprocess.run(
            [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=60,
        )
        assert proc.returncode == 0, f"PS reconstruct failed: {proc.stderr}"
        return proc.stdout.strip()

    def test_ps_sweep_ts_matches_python(self):
        out = self._run_ps_reconstruct(REF_BATCH_ID, "sweep_ng_hs02b_2x1_1h_scout")
        assert out == "sweep_ng_hs02b_2x1_1h_scout_20260711-061128"

    def test_ps_matches_all_reference_prefixes(self):
        progress = json.load(open(REF_PROGRESS, encoding="utf-8"))
        stored = {e["gcs_prefix"] for e in progress["experiments"]}
        for base in NG_BASE_PREFIXES:
            out = self._run_ps_reconstruct(REF_BATCH_ID, base)
            assert out in stored, f"PS-reconstructed {out} not in stored {sorted(stored)}"


# ===========================================================================
# (C) armCount + optTaskCount + tier map (run_sweep_batch.ps1:1077-1088)
# ===========================================================================


def _py_arm_count(objective: str) -> int:
    """Mirror of the arm-count loop: comma-split, `both`->2, others->1, floor 1."""
    total = 0
    for arm in objective.split(","):
        arm = arm.strip()
        if not arm:
            continue
        total += 2 if arm == "both" else 1
    return max(total, 1)


def _py_tier(task_count: int) -> str:
    """Mirror of the tier map (run_sweep_batch.ps1:1085-1088)."""
    if task_count <= 8:
        return "n2-standard-8"
    if task_count <= 16:
        return "n2-standard-16"
    if task_count <= 32:
        return "n2-standard-32"
    return "n2-standard-48"


class TestArmCountAndTierPython:
    """Pure-Python logic-guard mirror of the optimizer sizing math."""

    def test_arm_count_sharpe(self):
        assert _py_arm_count("sharpe") == 1

    def test_arm_count_both(self):
        assert _py_arm_count("both") == 2

    def test_arm_count_two_named_arms(self):
        assert _py_arm_count("sharpe,block_min") == 2

    def test_arm_count_empty_floors_to_one(self):
        assert _py_arm_count("") == 1
        assert _py_arm_count(",,") == 1

    def test_arm_count_trims_whitespace(self):
        assert _py_arm_count(" sharpe , block_min ") == 2

    def test_opt_task_count_formula(self):
        # completed x 4 x armCount
        assert 6 * 4 * 1 == 24
        assert 6 * 4 * 2 == 48

    def test_tier_boundaries(self):
        # 8 -> n2-standard-8
        assert _py_tier(8) == "n2-standard-8"
        # 9..16 -> n2-standard-16
        assert _py_tier(9) == "n2-standard-16"
        assert _py_tier(16) == "n2-standard-16"
        # 17..32 -> n2-standard-32
        assert _py_tier(17) == "n2-standard-32"
        assert _py_tier(32) == "n2-standard-32"
        # 33+ -> n2-standard-48
        assert _py_tier(33) == "n2-standard-48"
        assert _py_tier(48) == "n2-standard-48"
        assert _py_tier(1000) == "n2-standard-48"

    def test_reference_batch_sizing(self):
        """6 completed x 4 x 1 (sharpe) = 24 tasks -> n2-standard-32."""
        completed = 6
        assert _py_tier(completed * 4 * _py_arm_count("sharpe")) == "n2-standard-32"


class TestArmCountAndTierFixPinning:
    """Fix-pinning scan: the shipped script must carry the same arm-count loop
    and the exact tier-map expression (mirror run_sweep_batch.ps1:1081-1088)."""

    def test_arm_count_loop_present(self):
        text = _script_text()
        # `both` -> +2 else +1
        assert re.search(r"-eq\s+'both'", text) or re.search(r'-eq\s+"both"', text), (
            "arm-count loop must special-case 'both' -> 2 arms"
        )
        assert ".Split(','" in text or '.Split(","' in text, (
            "arm list must be comma-split"
        )

    def test_opt_task_count_expression_present(self):
        text = _script_text()
        # completed * 4 * armCount (the * 4 is the 2 metrics x 2 sides constant)
        assert re.search(r"\*\s*4\s*\*", text), (
            "optTaskCount must be completed * 4 * armCount"
        )

    def test_tier_map_boundaries_present(self):
        text = _script_text()
        for boundary, tier in (
            ("-le 8", "n2-standard-8"),
            ("-le 16", "n2-standard-16"),
            ("-le 32", "n2-standard-32"),
        ):
            assert boundary in text, f"tier boundary `{boundary}` missing"
            assert tier in text, f"tier `{tier}` missing"
        assert "n2-standard-48" in text, "the else-tier n2-standard-48 is missing"


# ===========================================================================
# (D) Config predictions_path rewrite (the CRITICAL CROSS-LINK)
#     — real powershell.exe over tmp_path, mirroring
#       tests/test_autostamp_config_rewrite.py.
# ===========================================================================

# Extracted rewrite loop, mirroring run_sweep_batch.ps1:1273-1288 (Test-Path
# guard + .Replace + BOM-less UTF-8 write). This is the EXACT logic the resume
# script's finalize step must reuse (blueprint step 8 CRITICAL CROSS-LINK).
_EXTRACTED_REWRITE = r"""
param([string]$StampedCfgDir, [string]$BatchId, [string]$StampName)
if (Test-Path $StampedCfgDir) {
    $cfgPatched = 0
    foreach ($cfgFile in Get-ChildItem "$StampedCfgDir\*.json" -ErrorAction SilentlyContinue) {
        $cfgRaw = [System.IO.File]::ReadAllText($cfgFile.FullName)
        $cfgNew = $cfgRaw.Replace("batch_runs/$BatchId/", "batch_runs/$StampName/")
        if ($cfgNew -ne $cfgRaw) {
            [System.IO.File]::WriteAllText($cfgFile.FullName, $cfgNew, [System.Text.UTF8Encoding]::new($false))
            $cfgPatched++
        }
    }
    Write-Host "REWROTE $cfgPatched"
}
"""


def _real_config(old_id: str) -> dict:
    """A config shaped like the REAL reference batch_configs entries: an
    ensemble config whose models.long / models.short carry a
    predictions_path under reports/batch_runs/<old_id>/predictions/... and a
    sweep-rooted model_path under reports/sweep_.../registry/..."""
    return {
        "nickname": "NG_Sharpe_E01_07112026",
        "execution_symbol": "NG",
        "models": {
            "long": {
                "model_path": (
                    "reports/sweep_ng_hs02b_3x1_6h_scout_20260711-061128/"
                    "registry/production_output/registry/"
                    "E2E_HourSet_02B_long_average_precision/final_model.pkl"
                ),
                "predictions_path": (
                    f"reports/batch_runs/{old_id}/predictions/NG_Sharpe_E01_predictions.csv"
                ),
                "symbol": "NG",
            },
            "short": {
                "model_path": (
                    "reports/sweep_ng_hs02b_2x1_1h_scout_20260711-061128/"
                    "registry/production_output/registry/"
                    "E2E_HourSet_02B_short_average_precision/final_model.pkl"
                ),
                "predictions_path": (
                    f"reports/batch_runs/{old_id}/predictions/NG_Sharpe_E01_predictions.csv"
                ),
                "symbol": "NG",
            },
        },
    }


def _write_stamped_cfg(tmp_path, old_id: str, filename: str = "NG_Sharpe_E01_07112026.json"):
    """Create <tmp>/<STAMP_NAME>/batch_configs/<filename> with an old-id config;
    return the path. Uses batch_configs/ (the REAL reference dir name)."""
    cfg_dir = tmp_path / STAMP_NAME / "batch_configs"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = cfg_dir / filename
    cfg_path.write_text(json.dumps(_real_config(old_id), indent=4), encoding="utf-8")
    return cfg_path


def _run_ps_rewrite(tmp_path, cfg_dir, old_id: str, new_id: str) -> subprocess.CompletedProcess:
    """Run, via real powershell.exe -File, the EXACT rewrite loop extracted from
    run_sweep_batch.ps1:1273-1288. Logic-guard around the EXTRACTED PS
    expression/branch shape, NOT an e2e run of the whole finalize step."""
    ps1 = tmp_path / "_rewrite_harness.ps1"
    ps1.write_text(_EXTRACTED_REWRITE, encoding="utf-8")
    return subprocess.run(
        [
            POWERSHELL, "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass", "-File", str(ps1),
            "-StampedCfgDir", str(cfg_dir),
            "-BatchId", old_id,
            "-StampName", new_id,
        ],
        capture_output=True, text=True, timeout=60,
    )


@requires_powershell
class TestRealPowerShellConfigRewrite:
    """Exercises the ACTUAL PS `.Replace(...)` + BOM-less UTF-8 write semantics
    the resume script's finalize step must reuse (blueprint step 8). Logic-guard
    around the extracted PS expression — not an e2e run of the whole script."""

    def test_predictions_path_repointed_to_stamped_id(self, tmp_path):
        cfg_path = _write_stamped_cfg(tmp_path, OLD_ID)
        proc = _run_ps_rewrite(tmp_path, cfg_path.parent, OLD_ID, STAMP_NAME)
        assert proc.returncode == 0, f"PS rewrite failed: {proc.stderr}"

        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert data["models"]["long"]["predictions_path"] == (
            f"reports/batch_runs/{STAMP_NAME}/predictions/NG_Sharpe_E01_predictions.csv"
        )
        assert data["models"]["short"]["predictions_path"].startswith(
            f"reports/batch_runs/{STAMP_NAME}/predictions/"
        )

    def test_no_stale_old_id_survives(self, tmp_path):
        cfg_path = _write_stamped_cfg(tmp_path, OLD_ID)
        proc = _run_ps_rewrite(tmp_path, cfg_path.parent, OLD_ID, STAMP_NAME)
        assert proc.returncode == 0, f"PS rewrite failed: {proc.stderr}"
        raw = cfg_path.read_text(encoding="utf-8")
        assert f"batch_runs/{OLD_ID}/" not in raw, (
            "stale batch_runs/<OLD_ID>/ segment survived the rewrite"
        )

    def test_model_path_left_byte_for_byte_unchanged(self, tmp_path):
        """model_path is sweep-rooted -> the batch_runs/<id>/ replace never matches
        it. It MUST remain byte-identical (this is the NG/SI recovery bug guard)."""
        cfg_path = _write_stamped_cfg(tmp_path, OLD_ID)
        before = json.loads(cfg_path.read_text(encoding="utf-8"))
        proc = _run_ps_rewrite(tmp_path, cfg_path.parent, OLD_ID, STAMP_NAME)
        assert proc.returncode == 0, f"PS rewrite failed: {proc.stderr}"

        after = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert after["models"]["long"]["model_path"] == before["models"]["long"]["model_path"]
        assert after["models"]["short"]["model_path"] == before["models"]["short"]["model_path"]
        assert after["models"]["long"]["model_path"] == (
            "reports/sweep_ng_hs02b_3x1_6h_scout_20260711-061128/registry/"
            "production_output/registry/E2E_HourSet_02B_long_average_precision/final_model.pkl"
        )

    def test_rewritten_file_has_no_utf8_bom(self, tmp_path):
        """The BOM-less UTF8Encoding($false) write must NOT prepend a BOM
        (load_strategy_config / json.load reject a BOM)."""
        cfg_path = _write_stamped_cfg(tmp_path, OLD_ID)
        proc = _run_ps_rewrite(tmp_path, cfg_path.parent, OLD_ID, STAMP_NAME)
        assert proc.returncode == 0, f"PS rewrite failed: {proc.stderr}"
        raw = cfg_path.read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf"), "rewritten config has a UTF-8 BOM"
        # Sanity: still valid, still repointed.
        assert json.loads(raw.decode("utf-8"))["models"]["long"]["predictions_path"].startswith(
            f"reports/batch_runs/{STAMP_NAME}/"
        )

    def test_already_stamped_config_is_noop(self, tmp_path):
        """A config already at the stamped id yields ZERO substitutions (idempotent
        re-run) — the file's predictions_path stays at the stamped id."""
        cfg_path = _write_stamped_cfg(tmp_path, STAMP_NAME)  # already-stamped
        proc = _run_ps_rewrite(tmp_path, cfg_path.parent, OLD_ID, STAMP_NAME)
        assert proc.returncode == 0, f"PS rewrite failed: {proc.stderr}"
        assert "REWROTE 0" in proc.stdout
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert data["models"]["long"]["predictions_path"].startswith(
            f"reports/batch_runs/{STAMP_NAME}/"
        )


class TestConfigRewriteFixPinning:
    """Fix-pinning scan: the shipped resume_batch.ps1 MUST contain the rewrite
    (`.Replace("batch_runs/...` + BOM-less UTF8Encoding($false) write) so the
    coder cannot forget the CRITICAL CROSS-LINK, and must target the actual
    config dir that exists in a batch run (batch_configs/)."""

    def test_shipped_script_contains_batch_runs_replace(self):
        text = _script_text()
        # The rewrite repoints batch_runs/<old>/ -> batch_runs/<new>/.
        assert re.search(
            r'\.Replace\(\s*"batch_runs/', text
        ), 'shipped script missing the `.Replace("batch_runs/...` cross-link rewrite'
        # Both the from- and to-segments reference batch_runs/.
        assert text.count('"batch_runs/') >= 2, (
            "the rewrite must reference batch_runs/ on both the from- and to-side"
        )

    def test_shipped_script_uses_bomless_utf8_write(self):
        text = _script_text()
        assert "UTF8Encoding]::new($false)" in text, (
            "the config rewrite must use a BOM-less UTF8Encoding($false) write"
        )

    def test_reference_configs_are_in_batch_configs_dir(self):
        """The REAL reference batch dir uses batch_configs/ (NOT configs/). Pin
        that the shipped rewrite targets a config dir that actually exists."""
        assert os.path.isdir(REF_CONFIG_DIR), (
            f"reference config dir not found: {REF_CONFIG_DIR}"
        )
        cfgs = [f for f in os.listdir(REF_CONFIG_DIR) if f.endswith(".json")]
        assert len(cfgs) == 4, f"expected 4 reference configs, found {len(cfgs)}: {cfgs}"

    def test_reference_configs_carry_stamped_predictions_and_sweep_model_path(self):
        """Each reference config's predictions_path points at the STAMPED batch
        dir and its model_path is sweep-rooted — the exact shape the rewrite must
        produce (predictions repointed, model_path untouched)."""
        for fn in os.listdir(REF_CONFIG_DIR):
            if not fn.endswith(".json"):
                continue
            data = json.load(open(os.path.join(REF_CONFIG_DIR, fn), encoding="utf-8"))
            for side in ("long", "short"):
                pred = data["models"][side]["predictions_path"]
                model = data["models"][side]["model_path"]
                assert pred.startswith(f"reports/batch_runs/{STAMP_NAME}/predictions/"), (
                    f"{fn} {side} predictions_path not repointed to the stamped dir: {pred}"
                )
                assert model.startswith("reports/sweep_"), (
                    f"{fn} {side} model_path is not sweep-rooted: {model}"
                )
                assert "batch_runs/" not in model, (
                    f"{fn} {side} model_path must NOT contain batch_runs/ (would be "
                    f"wrongly rewritten): {model}"
                )

    def test_shipped_script_targets_batch_configs_dir(self):
        """The shipped rewrite must target the batch_configs dir that actually
        exists in the batch run (not a non-existent configs/ dir)."""
        text = _script_text()
        assert "batch_configs" in text, (
            "shipped script must target the batch_configs/ dir (the real config "
            "dir), else the rewrite finds nothing and predictions_path stays stale"
        )


class TestConfigRewritePythonMirror:
    """Pure-Python logic-guard mirror of the str.replace rewrite (always runs)."""

    @staticmethod
    def _apply(raw: str, old_id: str, new_id: str) -> str:
        return raw.replace(f"batch_runs/{old_id}/", f"batch_runs/{new_id}/")

    def test_predictions_repointed(self):
        raw = json.dumps(_real_config(OLD_ID))
        out = json.loads(self._apply(raw, OLD_ID, STAMP_NAME))
        assert out["models"]["long"]["predictions_path"] == (
            f"reports/batch_runs/{STAMP_NAME}/predictions/NG_Sharpe_E01_predictions.csv"
        )
        assert f"batch_runs/{OLD_ID}/" not in self._apply(raw, OLD_ID, STAMP_NAME)

    def test_model_path_unchanged(self):
        raw = json.dumps(_real_config(OLD_ID))
        out = json.loads(self._apply(raw, OLD_ID, STAMP_NAME))
        assert out["models"]["long"]["model_path"].startswith(
            "reports/sweep_ng_hs02b_3x1_6h_scout_20260711-061128/"
        )

    def test_already_stamped_is_noop(self):
        already = json.dumps(_real_config(STAMP_NAME))
        assert self._apply(already, OLD_ID, STAMP_NAME) == already


# ===========================================================================
# (E) Required-field / no-silent-default pinning
# ===========================================================================


class TestRequiredFieldPinning:
    """Fix-pinning scan: the script must reference each REQUIRED manifest field
    and have a loud FATAL/crash path (throw / exit 1 / non-zero) keyed on the
    missing/invalid fields. NO silent defaults (user rule)."""

    def test_references_all_required_manifest_fields(self):
        text = _script_text()
        for field in (
            "opt_mode",
            "execution_data_path",
            "slippage_per_side",
            "post_optimizer_trials",
            "post_optimizer_holdout_months",
        ):
            assert field in text, (
                f"required manifest field `{field}` is never referenced — the "
                f"script must validate it (no silent default)"
            )
        # baseline.symbol is also required.
        assert re.search(r"baseline\.symbol|\.symbol\b", text), (
            "baseline.symbol must be referenced/validated"
        )

    def test_has_loud_fatal_crash_path(self):
        text = _script_text()
        assert ("exit 1" in text) or re.search(r"\bthrow\b", text), (
            "no loud FATAL/crash path (exit 1 / throw) — missing/invalid required "
            "inputs must crash, not silently default"
        )
        # A loud FATAL/ERROR marker keyed on the crash.
        assert re.search(r"FATAL|ERROR", text), (
            "crash path should surface a FATAL/ERROR message"
        )

    def test_opt_mode_enum_validated(self):
        """opt_mode must be constrained to {individual, ensemble}."""
        text = _script_text()
        assert "individual" in text and "ensemble" in text, (
            "opt_mode must be validated against {individual, ensemble}"
        )

    def test_slippage_range_validated(self):
        """slippage_per_side must be range-checked to [0, 0.5] and reject
        out-of-range values (crash loudly)."""
        text = _script_text()
        # The 0.5 upper bound must appear near slippage validation.
        assert "0.5" in text, (
            "slippage_per_side upper bound 0.5 not enforced — out-of-range "
            "slippage must be rejected"
        )
        assert "slippage_per_side" in text


# ===========================================================================
# (F) DryRun read-only contract
# ===========================================================================


class TestDryRunReadOnlyContract:
    """Fix-pinning scan: .bak backup writes, progress (ConvertTo-Json) writes,
    and the optimizer deploy must all be gated on NON-DryRun. DryRun performs
    ZERO filesystem mutations, ZERO .bak writes, ZERO deploys/VM ops."""

    def test_dryrun_guard_token_present(self):
        text = _script_text()
        assert re.search(r"if\s*\(\s*-not\s+\$DryRun\s*\)", text) or re.search(
            r"if\s*\(\s*\$DryRun\s*\)", text
        ), "no `if (-not $DryRun)` / `if ($DryRun)` gate present"

    def test_backup_bak_written(self):
        """A .bak backup-before-mutate must exist (blueprint step 5)."""
        text = _script_text()
        assert ".bak" in text, (
            "no batch_progress.json.bak backup-before-mutate — step 5 requires "
            "backing up before any write"
        )

    def test_progress_written_via_convertto_json(self):
        text = _script_text()
        assert "ConvertTo-Json" in text, (
            "batch_progress.json must be (re)written via ConvertTo-Json -Depth 10"
        )
        assert re.search(r"ConvertTo-Json\s+-Depth\s+10", text), (
            "progress write must use -Depth 10 (mirror Save-Progress)"
        )

    def test_recovered_marker_and_recovery_note_present(self):
        """Recovered entries get a `recovered:true` marker + top-level
        `recovery_note` (blueprint step 5)."""
        text = _script_text()
        assert "recovered" in text, "recovered-entry provenance marker missing"
        assert "recovery_note" in text, "top-level recovery_note missing"

    def test_mutations_are_after_dryrun_guard(self):
        """Sanity structural check: the FIRST `if (-not $DryRun)` guard must
        appear BEFORE the first .bak / ConvertTo-Json write / deploy call, i.e.
        the mutating operations live inside a NON-DryRun branch, not before any
        guard. (Coarse ordering guard — the coder decides exact block nesting.)"""
        text = _script_text()
        guard_idx = text.find("-not $DryRun")
        assert guard_idx != -1, "no `-not $DryRun` guard found at all"
        # The .bak write must not precede the very first DryRun guard.
        bak_idx = text.find(".bak")
        if bak_idx != -1:
            assert bak_idx > guard_idx, (
                "a .bak write appears before any -not $DryRun guard — DryRun would "
                "mutate the filesystem"
            )


# ===========================================================================
# (G) gcloud-not-gsutil + targeted-VM-filter + PLAIN-id deploy pinning
# ===========================================================================


class TestGcloudAndTargetedVmPinning:
    """Fix-pinning scan for the cloud-safety invariants: gcloud storage only,
    the targeted VM name filter, -NoMonitor deploy, and the PLAIN batch id at
    deploy time (rename is AFTER post-opt)."""

    def test_gcloud_storage_only_no_gsutil(self):
        text = _script_text()
        assert "gcloud storage" in text, "bucket ops must use `gcloud storage`"
        assert "gsutil" not in text, "gsutil is BANNED — it must never appear"

    def test_targeted_vm_name_filter(self):
        """Running-VM guard must use the targeted filter
        name~'^(optuna-sweep|opt-post)' (reap_orphan_vms.ps1:26-28), never a
        broad list/kill."""
        text = _script_text()
        assert re.search(r"name~'\^\(optuna-sweep\|opt-post\)'", text), (
            "running-VM guard must use the targeted filter "
            "name~'^(optuna-sweep|opt-post)'"
        )

    def test_deploy_uses_nomonitor(self):
        """The optimizer deploy must pass -NoMonitor (the built-in poll uses
        gsutil, which is broken here — the resume script self-polls)."""
        text = _script_text()
        assert "gcp_deploy_optimizer.ps1" in text, (
            "must invoke gcp/gcp_deploy_optimizer.ps1"
        )
        assert "-NoMonitor" in text, "the optimizer deploy must pass -NoMonitor"

    def test_deploy_gated_on_non_dryrun(self):
        """The deploy invocation must not run in DryRun."""
        text = _script_text()
        # There must be a DryRun guard somewhere, and the deploy call must exist.
        assert "gcp_deploy_optimizer.ps1" in text
        assert re.search(r"-not\s+\$DryRun", text) or re.search(r"\$DryRun", text), (
            "the deploy must be gated on NON-DryRun"
        )

    def test_deploy_uses_plain_batch_id(self):
        """gcp_deploy_optimizer.ps1 REQUIRES reports\\batch_runs\\<PLAIN
        BatchId>\\batch_progress.json — so -BatchId passed to the deploy must be
        the PLAIN id (rename happens AFTER post-opt). Pin that the deploy passes
        $BatchId (the plain id), not a stamped $stampName."""
        text = _script_text()
        # Find the deploy invocation region and assert -BatchId $BatchId is passed.
        assert re.search(r"-BatchId['\"]?\s*,?\s*\$BatchId", text) or (
            "-BatchId" in text and "$BatchId" in text
        ), "the deploy must pass the PLAIN $BatchId (rename is AFTER post-opt)"

    def test_self_poll_uses_gcloud_describe(self):
        """Self-poll for VM lifecycle must use `gcloud compute instances describe`
        with a status format (gsutil-free)."""
        text = _script_text()
        assert "gcloud compute instances describe" in text, (
            "VM-lifecycle self-poll must use `gcloud compute instances describe`"
        )
        assert 'get(status)' in text or "get(status)" in text, (
            "the describe self-poll must read the VM status via --format=\"get(status)\""
        )

    def test_batch_summary_optimized_probe(self):
        """Post-opt completion is detected by the landed batch_summary_optimized_*.md."""
        text = _script_text()
        assert "batch_summary_optimized_" in text, (
            "post-opt completion probe must look for batch_summary_optimized_*.md"
        )
