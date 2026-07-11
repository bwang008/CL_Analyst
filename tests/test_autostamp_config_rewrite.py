"""
TDD-TESTER AUTHORIZATION
Target Implementation File: gcp/run_sweep_batch.ps1
Target Class/Function: auto-stamp config-path rewrite block (~lines 1240-1295;
    rewrite loop :1272-1288 — the two additive `else` warning branches on
    `if (Test-Path $stampedCfgDir)` (:1273) and `if ($cfgPatched -gt 0)` (:1285))
Status: FINALIZED
Strict-Lock: TRUE (Implementation agents may NOT modify this file)

Ticket: autorename-stale-config-paths_07112026_1003

--------------------------------------------------------------------------------
WHAT THIS PINS
--------------------------------------------------------------------------------
After a completed batch dir `batch_<ts>` is auto-renamed to
`batch_<ts>_<SYMBOL>_<TIER>[_OBJAB]` (the `$stampName`), the auto-stamp block in
`gcp/run_sweep_batch.ps1` rewrites the self-referencing batch-dir path segment
inside the stamped ensemble config JSONs so the embedded `predictions_path`
stays loadable:

    $cfgNew = $cfgRaw.Replace("batch_runs/$BatchId/", "batch_runs/$stampName/")
    [System.IO.File]::WriteAllText($cfgFile.FullName, $cfgNew, [UTF8Encoding]::new($false))

`model_path` is sweep-rooted (reports/sweep_.../model.pkl) so the
`batch_runs/<id>/` replace NEVER matches it — it must stay byte-identical.

THE FIX (this ticket) adds two ADDITIVE, LOUD, Yellow `Write-Host` WARNING
`else` branches (they are WARNINGS, not throws — the block is inside a
never-fail try/catch; Phase-6 is the hard gate):
  (a) an `else` on `if (Test-Path $stampedCfgDir)`  — warns the stamped configs
      dir was NOT found and predictions_path was NOT rewritten (msg includes the
      offending `$stampedCfgDir` path).
  (b) an `else` on `if ($cfgPatched -gt 0)`          — warns ZERO config paths
      matched / were rewritten.

--------------------------------------------------------------------------------
STRATEGY  (see report for the justification)
--------------------------------------------------------------------------------
* STRATEGY A (chosen for the semantic tests): invoke REAL `powershell.exe` over a
  `tmp_path` fixture and exercise the EXACT extracted rewrite expression
  (`.Replace(...)` + BOM-less UTF-8 write, mirroring :1276-1281). This validates
  the ACTUAL PowerShell string-replace + BOM-less write semantics — repoint of
  `predictions_path`, byte-identical `model_path`, and no UTF-8 BOM. It ALSO
  drives the two `else`-branch warnings through PowerShell (missing configs dir /
  a config with no matching segment) and asserts the WARNING text on stdout.
  These are logic-guards around the EXTRACTED PS expression / branch shape, NOT
  an e2e run of the full 1240-1295 block. Every PowerShell test skips gracefully
  when `powershell.exe` is unavailable (it is available in this win32 env).

* FIX-PINNING (pure text scan of the shipped script, no PowerShell needed): reads
  `gcp/run_sweep_batch.ps1` and asserts the two `else` WARNING branches EXIST,
  keyed on STABLE distinctive substrings. These FAIL against the current script
  (the branches don't exist yet) so the suite is genuinely RED for the fix.

* STRATEGY B mirror (pure-Python LOGIC-GUARD, always runs): re-implements the
  identical `str.replace("batch_runs/{old}/", "batch_runs/{new}/")` over the same
  fixture. Explicitly a LOGIC-GUARD MIRROR, NOT e2e coverage of the PowerShell.

NONE of these tests touch real `reports/` dirs — everything uses `tmp_path`.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PS_SCRIPT = os.path.join(PROJECT_ROOT, "gcp", "run_sweep_batch.ps1")

# The batch-dir replace target string used by the shipped rewrite loop.
_REPLACE_FROM = "batch_runs/{old}/"
_REPLACE_TO = "batch_runs/{new}/"

# Canonical fixture ids (old = plain VM-baked id; new = auto-stamped name).
OLD_ID = "batch_20260711_120000"
NEW_ID = "batch_20260711_120000_CL_SCOUT_OBJAB"

POWERSHELL = shutil.which("powershell.exe") or shutil.which("powershell")
requires_powershell = pytest.mark.skipif(
    POWERSHELL is None,
    reason="powershell.exe not found on PATH (required for the real-PS rewrite tests)",
)


def _script_text() -> str:
    assert os.path.exists(PS_SCRIPT), f"missing target script: {PS_SCRIPT}"
    return open(PS_SCRIPT, encoding="utf-8-sig").read()


def _rewrite_region_text() -> str:
    """The auto-stamp rewrite region: from `if (Test-Path $stampedCfgDir)` up to
    (but not including) the OUTER symbol-check `else` — which is anchored on the
    stable `baseline.symbol unreadable` warning that immediately follows it.

    Anchoring on that unique line (NOT the first `} else {`) is deliberate: once
    the fix adds `else` branches INSIDE this region, a first-`} else {` boundary
    would truncate the region and hide the fix's own else. This boundary is
    stable across the fix so both the shipped-block harness and the text-scan
    fix-pinning tests see the WHOLE region including the new branches."""
    text = _script_text()
    start = text.index("if (Test-Path $stampedCfgDir)")
    # The outer symbol-check else opens right before this warning line.
    marker = text.index('WARNING: baseline.symbol unreadable', start)
    end = text.rindex("} else {", start, marker)
    return text[start:end]


# ---------------------------------------------------------------------------
# Shared fixture: a stamped configs dir with one ensemble config JSON that
# carries an OLD-batch-id predictions_path AND a sweep-rooted model_path.
# ---------------------------------------------------------------------------

def _ensemble_config(old_id: str) -> dict:
    """An ensemble config shaped like the VM bake: an ABSOLUTE-ish
    predictions_path under reports/batch_runs/<old_id>/predictions/... and a
    sweep-rooted model_path under reports/sweep_.../model.pkl."""
    return {
        "nickname": "cl_scout_ens_01",
        "predictions_path": f"reports/batch_runs/{old_id}/predictions/oos_predictions_expa_long.csv",
        "long": {
            "predictions_path": f"reports/batch_runs/{old_id}/predictions/oos_predictions_expa_long.csv",
            "model_path": "reports/sweep_20260710_090000_CL/models/long/model.pkl",
        },
        "short": {
            "predictions_path": f"reports/batch_runs/{old_id}/predictions/oos_predictions_expb_short.csv",
            "model_path": "reports/sweep_20260710_090000_CL/models/short/model.pkl",
        },
    }


def _write_stamped_cfg(tmp_path, old_id: str, filename: str = "cl_scout_ens_01.json"):
    """Create <tmp>/<new>/configs/<filename> with an old-id config; return the path."""
    cfg_dir = tmp_path / NEW_ID / "configs"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = cfg_dir / filename
    cfg_path.write_text(
        json.dumps(_ensemble_config(old_id), indent=2), encoding="utf-8"
    )
    return cfg_path


def _ps_file_run(tmp_path, script_body: str, cfg_dir, old_id, new_id):
    """Write `script_body` to a temp .ps1 and invoke it via
    `powershell.exe -File` so named parameters bind correctly (passing named
    args after `-Command <script>` does NOT bind — PS treats them as separate
    commands). The script MUST declare
    `param([string]$StampedCfgDir,[string]$BatchId,[string]$StampName)`.
    Returns the CompletedProcess (stdout/stderr carry Write-Host lines)."""
    ps1 = tmp_path / "_rewrite_harness.ps1"
    ps1.write_text(script_body, encoding="utf-8")
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


# Extracted rewrite loop, mirroring gcp/run_sweep_batch.ps1:1273-1288
# (Test-Path guard + .Replace + BOM-less UTF-8 write + $cfgPatched success line).
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
    if ($cfgPatched -gt 0) {
        Write-Host "REWROTE $cfgPatched"
    }
}
"""


def _run_ps_rewrite(tmp_path, cfg_dir, old_id: str, new_id: str) -> subprocess.CompletedProcess:
    """Run, via real powershell.exe, the EXACT rewrite loop extracted from
    gcp/run_sweep_batch.ps1:1273-1288. Logic-guard around the EXTRACTED PS
    expression/branch shape, NOT an e2e run of the full 1240-1295 block."""
    return _ps_file_run(tmp_path, _EXTRACTED_REWRITE, cfg_dir, old_id, new_id)


# ===========================================================================
# STRATEGY A — real powershell.exe against the EXTRACTED rewrite expression
# ===========================================================================


@requires_powershell
class TestRealPowerShellRewrite:
    """Exercises the ACTUAL PS `.Replace(...)` + BOM-less UTF-8 write semantics
    (mirroring gcp/run_sweep_batch.ps1:1276-1281). Logic-guard around the
    extracted PS expression — not an e2e run of the whole auto-stamp block."""

    def test_predictions_path_repointed_to_stamped_dir(self, tmp_path):
        """Every predictions_path old-id segment is repointed to the stamped id."""
        cfg_path = _write_stamped_cfg(tmp_path, OLD_ID)
        proc = _run_ps_rewrite(tmp_path, cfg_path.parent, OLD_ID, NEW_ID)
        assert proc.returncode == 0, f"PS rewrite failed: {proc.stderr}"

        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert data["predictions_path"] == (
            f"reports/batch_runs/{NEW_ID}/predictions/oos_predictions_expa_long.csv"
        )
        assert data["long"]["predictions_path"].startswith(
            f"reports/batch_runs/{NEW_ID}/predictions/"
        )
        assert data["short"]["predictions_path"].startswith(
            f"reports/batch_runs/{NEW_ID}/predictions/"
        )
        # No stale old-id path may survive anywhere in the file.
        assert f"batch_runs/{OLD_ID}/" not in cfg_path.read_text(encoding="utf-8")

    def test_model_path_left_byte_for_byte_unchanged(self, tmp_path):
        """model_path is sweep-rooted -> the batch_runs/<id>/ replace never
        matches it. It MUST remain byte-identical."""
        cfg_path = _write_stamped_cfg(tmp_path, OLD_ID)
        before = json.loads(cfg_path.read_text(encoding="utf-8"))
        proc = _run_ps_rewrite(tmp_path, cfg_path.parent, OLD_ID, NEW_ID)
        assert proc.returncode == 0, f"PS rewrite failed: {proc.stderr}"

        after = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert after["long"]["model_path"] == before["long"]["model_path"]
        assert after["short"]["model_path"] == before["short"]["model_path"]
        assert after["long"]["model_path"] == "reports/sweep_20260710_090000_CL/models/long/model.pkl"

    def test_rewritten_file_has_no_utf8_bom(self, tmp_path):
        """The BOM-less UTF8Encoding($false) write must NOT prepend a BOM
        (load_strategy_config / json.load reject a BOM)."""
        cfg_path = _write_stamped_cfg(tmp_path, OLD_ID)
        proc = _run_ps_rewrite(tmp_path, cfg_path.parent, OLD_ID, NEW_ID)
        assert proc.returncode == 0, f"PS rewrite failed: {proc.stderr}"

        raw = cfg_path.read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf"), "rewritten config has a UTF-8 BOM"
        # Sanity: still valid, still repointed.
        assert json.loads(raw.decode("utf-8"))["predictions_path"].startswith(
            f"reports/batch_runs/{NEW_ID}/"
        )

    def test_success_line_emitted_when_paths_matched(self, tmp_path):
        """The >0 branch fires its success line when at least one config matched."""
        cfg_path = _write_stamped_cfg(tmp_path, OLD_ID)
        proc = _run_ps_rewrite(tmp_path, cfg_path.parent, OLD_ID, NEW_ID)
        assert proc.returncode == 0, f"PS rewrite failed: {proc.stderr}"
        # our extracted harness prints "REWROTE <n>" from the >0 branch
        assert "REWROTE 1" in proc.stdout


@requires_powershell
class TestRealPowerShellWarningBranches:
    """Drives the two `else` warning conditions through real PowerShell against
    the SHIPPED warning wording (extracted from gcp/run_sweep_batch.ps1). These
    FAIL until the Coder adds the two `else` Write-Host branches, because the
    warning-substring assertions below key on text that does not exist yet."""

    @staticmethod
    def _shipped_rewrite_block() -> str:
        """The rewrite block AS SHIPPED in gcp/run_sweep_batch.ps1 (:1272-1288
        equivalent, parameterized), so we run the operator's REAL warning
        wording rather than a stand-in. If the `else` branches are absent, the
        warning simply never prints and the assertions go RED.

        We keep everything from `if (Test-Path $stampedCfgDir)` onward verbatim,
        and inject a param() header supplying $stampedCfgDir/$BatchId/$stampName
        in place of the `$stampedCfgDir = Join-Path ...` derivation line (which
        needs $BatchDir we don't have)."""
        block = _rewrite_region_text()  # the WHOLE region, incl. any fix else
        return (
            "param([string]$StampedCfgDir, [string]$BatchId, [string]$StampName)\n"
            "$stampedCfgDir = $StampedCfgDir\n"
            "$stampName = $StampName\n"
            + block
        )

    def _run_shipped_block(self, tmp_path, cfg_dir, old_id, new_id):
        return _ps_file_run(
            tmp_path, self._shipped_rewrite_block(), cfg_dir, old_id, new_id
        )

    def test_missing_configs_dir_warns(self, tmp_path):
        """RED until fix: Test-Path $stampedCfgDir is false -> a LOUD warning
        naming predictions_path and the missing dir must be printed."""
        missing = tmp_path / NEW_ID / "configs"  # never created
        proc = self._run_shipped_block(tmp_path, missing, OLD_ID, NEW_ID)
        combined = (proc.stdout + proc.stderr).lower()
        assert "predictions_path" in combined, (
            "missing-configs-dir `else` warning (mentioning predictions_path) "
            "was not emitted — the fix's (a) branch is absent"
        )
        assert ("not found" in combined) or ("not rewritten" in combined) \
            or ("was not found" in combined), (
            "missing-configs-dir warning must state the dir was not found / "
            "paths were not rewritten"
        )
        # The offending dir path must be surfaced so the warning is actionable.
        assert "configs" in combined

    def test_zero_configs_matched_warns(self, tmp_path):
        """RED until fix: the configs dir EXISTS but no config contains the
        batch_runs/<old>/ segment (cfgPatched stays 0) -> a LOUD warning that
        ZERO paths were rewritten must be printed."""
        cfg_dir = tmp_path / NEW_ID / "configs"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        # A config with NO matching old-id segment (already-stamped / unrelated).
        (cfg_dir / "already_ok.json").write_text(
            json.dumps({
                "predictions_path": f"reports/batch_runs/{NEW_ID}/predictions/x.csv",
                "model_path": "reports/sweep_20260710_090000_CL/models/long/model.pkl",
            }),
            encoding="utf-8",
        )
        proc = self._run_shipped_block(tmp_path, cfg_dir, OLD_ID, NEW_ID)
        combined = (proc.stdout + proc.stderr).lower()
        assert ("0 config" in combined) or ("zero config" in combined) \
            or ("no config" in combined) or ("were rewritten" in combined), (
            "zero-configs-matched `else` warning was not emitted — the fix's "
            "(b) branch is absent"
        )


# ===========================================================================
# FIX-PINNING — pure text scan of the shipped script (no PowerShell needed).
# These GUARANTEE the suite is RED for the fix: the two `else` warning branches
# do not exist yet, so these fail against the current gcp/run_sweep_batch.ps1.
# ===========================================================================


class TestFixPinningScriptText:
    """Reads gcp/run_sweep_batch.ps1 and asserts the two additive `else`
    WARNING branches are present, keyed on STABLE distinctive substrings.
    Currently RED (the branches are unimplemented)."""

    def test_missing_configs_dir_else_branch_exists(self):
        """(a) An `else` on `if (Test-Path $stampedCfgDir)` must emit a Yellow
        Write-Host WARNING mentioning predictions_path and the missing dir."""
        region = _rewrite_region_text()
        # There must be an `else` inside the rewrite region (the current script
        # has NONE here — the outer symbol-check else is OUTSIDE this region),
        # and the region must carry a Yellow warning naming predictions_path.
        assert re.search(r"}\s*else\s*{", region), (
            "no `else` branch found in the rewrite region — the missing "
            "-configs-dir warning (fix branch a) has not been added"
        )
        assert "predictions_path" in region, (
            "the missing-configs-dir `else` must mention predictions_path so "
            "the operator knows the repoint was skipped"
        )
        assert "-ForegroundColor Yellow" in region, (
            "the added warnings must be LOUD (Write-Host -ForegroundColor Yellow)"
        )

    def test_zero_patched_else_branch_exists(self):
        """(b) An `else` on `if ($cfgPatched -gt 0)` must emit a Yellow
        Write-Host WARNING that ZERO config paths matched / were rewritten."""
        region = _rewrite_region_text()
        # Find the `if ($cfgPatched -gt 0) { ... }` and require an `else` after it.
        m = re.search(r"if\s*\(\s*\$cfgPatched\s*-gt\s*0\s*\)", region)
        assert m is not None, "the $cfgPatched success-line if was removed/renamed"
        tail = region[m.end():]
        assert re.search(r"}\s*else\s*{", tail), (
            "no `else` on `if ($cfgPatched -gt 0)` — the zero-configs-matched "
            "warning (fix branch b) has not been added"
        )
        # A distinctive zero/none wording for the warning.
        tail_low = tail.lower()
        assert ("0 config" in tail_low) or ("zero" in tail_low) \
            or ("no config" in tail_low) or ("none" in tail_low), (
            "the zero-patched `else` must state that ZERO/none config paths were "
            "matched / rewritten"
        )

    def test_rewrite_replace_and_bomless_write_preserved(self):
        """Guard rail (should already PASS): the fix is purely additive — the
        core `.Replace(...)` and the BOM-less UTF-8 write must remain intact."""
        text = _script_text()
        assert '.Replace("batch_runs/$BatchId/", "batch_runs/$stampName/")' in text, (
            "the core batch-dir replace was altered — the fix must be additive "
            "only (model_path must stay untouched)"
        )
        assert "System.Text.UTF8Encoding]::new($false)" in text, (
            "the BOM-less UTF-8 write was altered — must stay BOM-less"
        )


# ===========================================================================
# STRATEGY B mirror — pure-Python LOGIC-GUARD (always runs; NOT e2e of the PS).
# ===========================================================================


class TestPythonLogicGuardMirror:
    """Re-implements the identical str.replace("batch_runs/{old}/",
    "batch_runs/{new}/") against the fixture. This is a LOGIC-GUARD MIRROR of
    the PowerShell rewrite semantics — it does NOT execute or cover the actual
    PowerShell auto-stamp block."""

    @staticmethod
    def _apply(raw: str, old_id: str, new_id: str) -> str:
        return raw.replace(
            _REPLACE_FROM.format(old=old_id), _REPLACE_TO.format(new=new_id)
        )

    def test_predictions_path_repointed(self):
        raw = json.dumps(_ensemble_config(OLD_ID))
        out = json.loads(self._apply(raw, OLD_ID, NEW_ID))
        assert out["predictions_path"] == (
            f"reports/batch_runs/{NEW_ID}/predictions/oos_predictions_expa_long.csv"
        )
        assert f"batch_runs/{OLD_ID}/" not in self._apply(raw, OLD_ID, NEW_ID)

    def test_model_path_unchanged(self):
        raw = json.dumps(_ensemble_config(OLD_ID))
        out = json.loads(self._apply(raw, OLD_ID, NEW_ID))
        assert out["long"]["model_path"] == "reports/sweep_20260710_090000_CL/models/long/model.pkl"
        assert out["short"]["model_path"] == "reports/sweep_20260710_090000_CL/models/short/model.pkl"

    def test_no_match_is_a_noop(self):
        """A config already at the stamped id (or unrelated) yields ZERO
        substitutions — this is the exact condition the fix's (b) warning
        surfaces."""
        already = json.dumps({
            "predictions_path": f"reports/batch_runs/{NEW_ID}/predictions/x.csv",
            "model_path": "reports/sweep_20260710_090000_CL/models/long/model.pkl",
        })
        assert self._apply(already, OLD_ID, NEW_ID) == already
