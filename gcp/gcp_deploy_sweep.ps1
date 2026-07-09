<#
.SYNOPSIS
    Provisions a sweep VM, uploads code + downloads data from GCS, and launches the sweep pipeline.
.DESCRIPTION
    Unified deployment for batch sweep experiments.
#>

param (
    [Parameter(Mandatory=$true)][string]$VmName,
    [Parameter(Mandatory=$true)][string]$MasterConfig,
    [string]$Zone = "us-east4-a",
    [string]$MachineType = "c2-standard-16",
    [string]$ProvisioningModel = "STANDARD",
    [string]$JobName = ""
)

# Enforce required gcloud SSH config for Windows/PLink
$forceConnect = gcloud config get ssh/putty_force_connect --quiet 2>$null
if ($forceConnect -ne "True") {
    Write-Host "Setting ssh/putty_force_connect=True (required for Windows PLink)..." -ForegroundColor Yellow
    gcloud config set ssh/putty_force_connect True --quiet
}

# Add gcloud to PATH if not already there
$gcloudBin = "C:\Users\bwang\AppData\Local\Google\Cloud SDK\google-cloud-sdk\bin"
if ($env:PATH -notlike "*$gcloudBin*") {
    $env:PATH = "$gcloudBin;$env:PATH"
}

$ErrorActionPreference = "Continue"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Split-Path -Parent $ScriptDir
$GcpUser = $env:USERNAME
$RemoteHome = "/home/${GcpUser}"
$RemoteProject = "${RemoteHome}/project"

# Internal/default variables
$DiskSizeGB = 100
$SkipProvision = $false
$NoShutdown = $false
$Project = gcloud config get-value project --quiet

# Parse the $MasterConfig JSON directly
$cfg = Get-Content $MasterConfig -Raw | ConvertFrom-Json
$symbol = $cfg.symbol
$datasetVersion = $cfg.data_workflow.dataset_version

# Avoid redundant prefixing if version already starts with symbol
if ($datasetVersion.ToUpper().StartsWith($symbol.ToUpper())) {
    $datasetName = "${datasetVersion}.parquet"
} else {
    $datasetName = "${symbol}_${datasetVersion}.parquet"
}

$GcsDataPath = "gs://cltrainer-optuna-results/data/$datasetName"
$StrategyConfig = Split-Path -Leaf $cfg.execution_workflow.strategy_config_path

$DataFileName = Split-Path -Leaf $GcsDataPath

Write-Host ""
Write-Host "=====================================================" -ForegroundColor Magenta
Write-Host " DEPLOY SWEEP PIPELINE" -ForegroundColor Magenta
Write-Host "=====================================================" -ForegroundColor Magenta
Write-Host "  VM:            $VmName"
Write-Host "  Machine:       $MachineType"
Write-Host "  Pricing:       $ProvisioningModel"
Write-Host "  Data (GCS):    $GcsDataPath"
Write-Host "  Strategy:      $StrategyConfig"
Write-Host "  Auto-Shutdown: $(-not $NoShutdown)"
Write-Host "=====================================================" -ForegroundColor Magenta

# --- [1/6] Provision VM (or reuse existing) ---
if (-not $SkipProvision) {
    Write-Host "`n[1/6] Provisioning canary VM..."
    
    # Check if VM already exists
    $existingStatus = $null
    try {
        $existingStatus = gcloud compute instances describe $VmName --zone=$Zone `
            --format="get(status)" 2>&1 | Select-String -Pattern "^(RUNNING|TERMINATED|STOPPED|SUSPENDED)$"
        if ($existingStatus) { $existingStatus = $existingStatus.ToString().Trim() }
    } catch {
        $existingStatus = $null
    }
    
    if ($existingStatus) {
        Write-Host "  VM already exists (status: $existingStatus)" -ForegroundColor Yellow
        if ($existingStatus -ne "RUNNING") {
            Write-Host "  Starting existing VM..." -ForegroundColor Yellow
            gcloud compute instances start $VmName --zone=$Zone --quiet
            Start-Sleep -Seconds 20
        }
    } else {
        Write-Host "  Creating new VM ($MachineType, $ProvisioningModel)..."
        $startupScript = Join-Path $ScriptDir "vm_startup.sh"
        
        # Build gcloud create args.
        # ORPHAN BACKSTOP: teardown normally comes from the LOCAL monitor process;
        # if that dies (reboot, killed terminal, IDE refresh) the VM would spin
        # forever. --max-run-duration is enforced by the GCP control plane, so the
        # VM dies at the deadline no matter what happens on this machine.
        # 480m >> the 360m experiment timeout — it is a backstop, not a scheduler.
        $createArgs = @(
            "compute", "instances", "create", $VmName,
            "--project=$Project",
            "--zone=$Zone",
            "--machine-type=$MachineType",
            "--image-family=ubuntu-2204-lts",
            "--image-project=ubuntu-os-cloud",
            "--boot-disk-size=${DiskSizeGB}GB",
            "--boot-disk-type=pd-ssd",
            "--metadata-from-file=startup-script=$startupScript",
            "--scopes=storage-full",
            "--max-run-duration=480m",
            "--quiet"
        )
        if ($ProvisioningModel -eq "SPOT") {
            $createArgs += "--provisioning-model=SPOT"
            # STOP preserved for preemption semantics (zone-failover relies on it);
            # at max-run-duration expiry the VM stops -> compute billing ends.
            $createArgs += "--instance-termination-action=STOP"
        } else {
            # STANDARD: hard-DELETE at the deadline (disk + IP freed too).
            $createArgs += "--instance-termination-action=DELETE"
        }
        
        & gcloud @createArgs
        
        if ($LASTEXITCODE -ne 0) {
            Write-Host "`nERROR: Failed to create VM." -ForegroundColor Red
            Write-Host "  Check quota: gcloud compute regions describe $Zone --format='table(quotas)'"
            exit 1
        }
        Write-Host "  VM created!" -ForegroundColor Green
    }
    
    # NOTE: SSH host key acceptance is handled by gcloud config:
    #   gcloud config set ssh/putty_force_connect True
    # This makes --quiet auto-accept PLink host key prompts on Windows.

    # Wait for startup script
    Write-Host "  Waiting for startup script to complete..."
    $maxWait = 600; $elapsed = 0
    while ($elapsed -lt $maxWait) {
        Start-Sleep -Seconds 15; $elapsed += 15
        try {
            $ready = gcloud compute ssh $VmName --zone=$Zone `
                --command="test -f /tmp/startup_done && echo READY" `
                --quiet 2>$null
            if ($ready -match "READY") { break }
        } catch {}
        Write-Host "  Installing... (${elapsed}s elapsed)" -ForegroundColor Gray
    }
    if ($elapsed -ge $maxWait) {
        Write-Host "  WARNING: Reached ${maxWait}s timeout. Proceeding anyway." -ForegroundColor Yellow
    } else {
        Write-Host "  VM is ready!" -ForegroundColor Green
    }
} else {
    Write-Host "`n[1/6] Skipping provision (-SkipProvision)" -ForegroundColor Yellow
}

# --- [2/6] Create directory structure ---
Write-Host "`n[2/6] Creating directory structure..."
gcloud compute ssh $VmName --zone=$Zone --quiet `
    --command="mkdir -p $RemoteProject/agent $RemoteProject/src/live_execution/strategies $RemoteProject/src/features $RemoteProject/src/config $RemoteProject/src/core $RemoteProject/gcp $RemoteProject/configs/strategies $RemoteProject/configs/sweeps $RemoteProject/models/optuna_studies $RemoteProject/reports $RemoteHome/data" 2>$null

# --- [3/6] Upload code ---
Write-Host "`n[3/6] Uploading code..."

# Unique per-VM archive name: concurrent sweep deploys (Start-Job) previously raced a
# single shared deploy.zip, clobbering each other so some VMs got an empty/corrupt zip
# and the sweep tmux session died instantly (STARTUP_TIMEOUT). Never share the filename.
$zipName = "deploy_$VmName.zip"
$zipFile = Join-Path $ProjectDir $zipName
if (Test-Path $zipFile) { Remove-Item $zipFile -Force }

$itemsToZip = @("src", "agent", "gcp", "requirements.txt", ".env")
$existingItems = @()
foreach ($item in $itemsToZip) {
    if (Test-Path (Join-Path $ProjectDir $item)) {
        $existingItems += $item
    }
}

if ($existingItems.Count -gt 0) {
    Write-Host "  Zipping codebase (excluding pycache and datasets)..."
    # -C + absolute -f: CWD-independent and unique, so parallel deploys can't collide.
    $tarArgs = @("-a", "-c", "-C", $ProjectDir, "-f", $zipFile, "--exclude=__pycache__", "--exclude=*.parquet", "--exclude=*.csv") + $existingItems
    & "tar.exe" $tarArgs

    # Upload + unzip + VERIFY with retries. A silent scp failure used to leave ~/project
    # empty and the deploy marched on to launch tmux on missing code. Never do that again:
    # re-create the dir tree, re-upload, and confirm gcp/vm_sweep_run.sh actually exists.
    $codeLanded = $false
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        Write-Host "  Uploading code to VM (attempt $attempt/3)..."
        gcloud compute ssh $VmName --zone=$Zone --command="mkdir -p $RemoteProject $RemoteHome/data" --quiet 2>$null
        gcloud compute scp "$zipFile" "${VmName}:${RemoteProject}/$zipName" --zone=$Zone --quiet 2>$null
        gcloud compute ssh $VmName --zone=$Zone --command="cd $RemoteProject && unzip -q -o $zipName && rm -f $zipName" --quiet 2>$null
        $verify = gcloud compute ssh $VmName --zone=$Zone --command="test -f $RemoteProject/gcp/vm_sweep_run.sh && echo CODE_OK" --quiet 2>$null
        if ($verify -match "CODE_OK") { $codeLanded = $true; break }
        Write-Host "  Code not present on VM after attempt $attempt; retrying..." -ForegroundColor Yellow
        Start-Sleep -Seconds 3
    }
    Remove-Item $zipFile -ErrorAction SilentlyContinue

    if (-not $codeLanded) {
        Write-Host "  FATAL: code failed to land on VM (gcp/vm_sweep_run.sh missing after 3 attempts)." -ForegroundColor Red
        Write-Host "  Aborting deploy to avoid launching the sweep on an empty VM." -ForegroundColor Red
        exit 1
    }
    Write-Host "  Code verified on VM (gcp/vm_sweep_run.sh present)." -ForegroundColor Green
} else {
    Write-Host "  FATAL: No code directories found to upload locally." -ForegroundColor Red
    exit 1
}

# Upload resolved master config JSON to the VM (essential -- the sweep can't run without it).
# Verify it lands; a silent failure here is as fatal as missing code.
$remoteConfigDir = "$RemoteProject/configs/sweeps"
$masterLeaf = Split-Path -Leaf $MasterConfig
$remoteConfigPath = "${remoteConfigDir}/$masterLeaf"
$cfgLanded = $false
for ($attempt = 1; $attempt -le 3; $attempt++) {
    gcloud compute ssh $VmName --zone=$Zone --command="mkdir -p $remoteConfigDir" --quiet 2>$null
    gcloud compute scp $MasterConfig "${VmName}:${remoteConfigPath}" --zone=$Zone --quiet 2>$null
    $verifyCfg = gcloud compute ssh $VmName --zone=$Zone --command="test -f $remoteConfigPath && echo CFG_OK" --quiet 2>$null
    if ($verifyCfg -match "CFG_OK") { $cfgLanded = $true; break }
    Write-Host "  MasterConfig not present on VM after attempt $attempt; retrying..." -ForegroundColor Yellow
    Start-Sleep -Seconds 3
}
if (-not $cfgLanded) {
    Write-Host "  FATAL: MasterConfig ($masterLeaf) failed to land on VM after 3 attempts. Aborting deploy." -ForegroundColor Red
    exit 1
}
Write-Host "  Uploaded + verified MasterConfig: $masterLeaf" -ForegroundColor Green

# Upload strategy config
$configPath = Join-Path $ProjectDir "configs\strategies\$StrategyConfig"
if (Test-Path $configPath) {
    try {
        gcloud compute scp "$configPath" "${VmName}:${RemoteProject}/configs/strategies/" --zone=$Zone --quiet 2>$null
    } catch {
        Write-Host "  Retrying scp for $StrategyConfig..." -ForegroundColor Yellow
        Start-Sleep -Seconds 2
        try { gcloud compute scp "$configPath" "${VmName}:${RemoteProject}/configs/strategies/" --zone=$Zone --quiet 2>$null } catch { Write-Host "  Failed to copy $StrategyConfig" -ForegroundColor Red }
    }
    Write-Host "  Uploaded strategy config: $StrategyConfig"
}

Write-Host "  Code uploaded!" -ForegroundColor Green

# Fix CRLF line endings on shell scripts (Windows git checkout produces CRLF which breaks bash)
Write-Host "  Fixing line endings on shell scripts..."
try {
    gcloud compute ssh $VmName --zone=$Zone --command="find $RemoteProject -name '*.sh' -exec sed -i 's/\r$//' {} + && chmod +x $RemoteProject/gcp/*.sh" --quiet 2>$null
} catch {}
Write-Host "  Line endings fixed." -ForegroundColor Green

# --- [4/6] Download data from GCS to VM ---
Write-Host "`n[4/6] Downloading data from GCS to VM..."
Write-Host "  Source: $GcsDataPath"
$downloadCmd = "gsutil cp '$GcsDataPath' '$RemoteHome/data/$DataFileName'"
# Capture output + exit code (mirrors the [5/6] pattern below). A missing GCS object makes the
# remote `gsutil cp` fail with "No URLs matched" -- this is NON-RETRYABLE (the object is absent
# in every zone), so fail loudly here instead of marching on and letting it resurface two stages
# later at [6/6] mis-classified as a generic STARTUP_TIMEOUT.
$downloadOutput = gcloud compute ssh $VmName --zone=$Zone --command=$downloadCmd --quiet 2>&1
$downloadExit   = $LASTEXITCODE
$downloadText   = ($downloadOutput | Out-String)
if ($downloadExit -ne 0 -or $downloadText -match "No URLs matched|CommandException") {
    Write-Host "  FATAL: DATA_MISSING -- GCS data download failed for $GcsDataPath" -ForegroundColor Red
    Write-Host "  The object is absent in the bucket; it will be missing in every zone (non-retryable)." -ForegroundColor Red
    Write-Host "  Output: $downloadText" -ForegroundColor DarkGray
    exit 1
}
Write-Host "  Data ready on VM!" -ForegroundColor Green

# --- [5/6] Launch sweep run ---
Write-Host "`n[5/6] Launching sweep pipeline in tmux..."

$launchCmd = "tmux kill-session -t sweep 2>/dev/null; tmux new-session -d -s sweep 'cd $RemoteProject && bash gcp/vm_sweep_run.sh --master-config=configs/sweeps/$(Split-Path -Leaf $MasterConfig) --gcs-data-bucket=gs://cltrainer-optuna-results/data --gcs-prefix=$JobName --shutdown'"

# Execute and capture both streams with explicit exit code handling
$launchOutput = gcloud compute ssh $VmName --zone=$Zone --command=$launchCmd --quiet 2>&1
$sshExitCode = $LASTEXITCODE

if ($sshExitCode -ne 0) {
    Write-Host "  WARNING: SSH launch returned exit code $sshExitCode" -ForegroundColor Yellow
    Write-Host "  Output: $($launchOutput | Out-String)" -ForegroundColor DarkGray
}

# --- [6/6] Verify ---
Write-Host "`n[6/6] Verifying tmux session..."
Start-Sleep -Seconds 10
$tmuxCheck = gcloud compute ssh $VmName --zone=$Zone `
    --command="tmux has-session -t sweep 2>/dev/null && echo RUNNING" `
    --quiet 2>$null

if ($sshExitCode -eq 0 -and $tmuxCheck -match "RUNNING") {
    Write-Host "  tmux session 'sweep' is active!" -ForegroundColor Green
} else {
    Write-Host "  FATAL: Failed to initialize remote execution environment." -ForegroundColor Red
    Write-Host "  SSH Exit Code: $sshExitCode" -ForegroundColor Red
    Write-Host "  Output: $($launchOutput | Out-String)" -ForegroundColor DarkGray
    Write-Host "  Debug with: gcloud compute ssh $VmName --command='tmux attach -t sweep'"
    exit 1
}

Write-Host ""
Write-Host "=====================================================" -ForegroundColor Green
Write-Host " SWEEP PIPELINE LAUNCHED" -ForegroundColor Green
Write-Host "=====================================================" -ForegroundColor Green
Write-Host ""
Write-Host "  VM:           $VmName"
Write-Host "  Expected:     ~hours"
$gcsOut = "gs://cltrainer-optuna-results/$JobName/"
Write-Host "  GCS output:   $gcsOut"
Write-Host ""
Write-Host "Useful commands:" -ForegroundColor Cyan
$dlCmd = "gsutil -m cp -r $gcsOut ./"
Write-Host "  Check status:     .\gcp\gcp_check_status.ps1 -VmName $VmName"
Write-Host "  View live output: gcloud compute ssh $VmName --zone=$Zone --command='tmux attach -t sweep'"
Write-Host "  Download results: $dlCmd"
Write-Host ""
