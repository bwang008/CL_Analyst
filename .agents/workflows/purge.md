---
name: /purge
description: Run the repository maintenance cleanup script to clear unreferenced hyperparameter sweep artifacts and logs.
---

# Purge Workflow

This workflow executes the safe cleanup maintenance script to free up disk space in the repository. It protects models that are actively referenced in `reports/batch_runs` and purges unreferenced "junk" artifacts.

## Step 1: Calculate Total Repository Size
Run the following PowerShell command to calculate the current size of the repository before the purge:
```powershell
$size = (Get-ChildItem -Recurse -File -Force -ErrorAction SilentlyContinue | Measure-Object -Property Length -Sum).Sum / 1MB
Write-Host "Total Repository Size: {0:N2} MB" -f $size
```

## Step 2: Dry Run the Cleanup Script
Run the cleanup script in dry-run mode to see what will be deleted. The default cutoff is 3 days. If the user specifies a different cutoff (e.g. `--days 0`), use that instead.
```powershell
python scripts/maintenance_cleanup.py --dry-run --days 3
```

## Step 3: Present Analysis to User
Present a summary to the user containing:
1. The **current total size** of the repository (from Step 1).
2. The **total space that would be saved** (from Step 2).
3. The **estimated new size** of the repository after cleanup.
4. A brief breakdown of the types of files targeted (e.g., sweep zip artifacts, older log files).

Wait for the user's explicit confirmation before proceeding.

## Step 4: Execute Cleanup
If the user approves the dry run, execute the actual cleanup:
```powershell
python scripts/maintenance_cleanup.py --days 3
```

## Step 5: Wrap Up
Inform the user that the cleanup is complete and the repository space has been reclaimed.
