# generate-cloud.ps1 — POST a local batch.json to the ACA /generate endpoint
# and save every returned image to disk.
#
# Usage:
#   ./scripts/generate-cloud.ps1 -Url <container-app-root-url> -BatchFile <batch-file> [-OutputDir <output-dir>] [-Warmup]
#
# Example:
#   ./scripts/generate-cloud.ps1 -Url https://sdxl-generation-api.xxx.eastus.azurecontainerapps.io -BatchFile batch.json -OutputDir ./outputs
#   ./scripts/generate-cloud.ps1 -Url https://sdxl-generation-api.xxx.eastus.azurecontainerapps.io -BatchFile batch.json -OutputDir ./outputs -Warmup

param(
    [Parameter(Mandatory)]
    [string]$Url,

    [Parameter(Mandatory)]
    [string]$BatchFile,

    [string]$OutputDir = ".",

    [switch]$Warmup
)

# ── normalize URL ─────────────────────────────────────────────────────────────
$RootUrl     = $Url.TrimEnd('/')
$GenerateUrl = "$RootUrl/generate"
$HealthUrl   = "$RootUrl/health"
$PullUrl     = "$RootUrl/model/pull"
$StatusUrl   = "$RootUrl/model/status"

# ── validate batch file ───────────────────────────────────────────────────────
if (-not (Test-Path -LiteralPath $BatchFile -PathType Leaf)) {
    Write-Error "Batch file not found: $BatchFile"
    exit 1
}

# ── create output directory ───────────────────────────────────────────────────
if (-not (Test-Path -LiteralPath $OutputDir)) {
    New-Item -ItemType Directory -Path $OutputDir | Out-Null
}

# ── cold-start health poll ────────────────────────────────────────────────────
$MaxAttempts = 20
$SleepSeconds = 15
Write-Host "Polling health at $HealthUrl (up to $MaxAttempts attempts, ${SleepSeconds}s apart)..."

$healthy = $false
for ($i = 1; $i -le $MaxAttempts; $i++) {
    try {
        $h = Invoke-RestMethod -Uri $HealthUrl -Method Get -TimeoutSec 10 -ErrorAction Stop
        if ($h -ne $null) {
            Write-Host "Container is healthy (attempt $i)."
            $healthy = $true
            break
        }
    }
    catch {
        Write-Host "  Attempt $i/$MaxAttempts — not healthy yet; waiting ${SleepSeconds}s for cold start..."
    }
    Start-Sleep -Seconds $SleepSeconds
}

if (-not $healthy) {
    Write-Warning "Container did not report healthy after $MaxAttempts attempts. Proceeding anyway..."
}

# ── warm-up (opt-in) ──────────────────────────────────────────────────────────
if ($Warmup) {
    Write-Host "Warm-up requested — pulling model..."
    try {
        Invoke-RestMethod -Uri $PullUrl -Method Post -TimeoutSec 30 | Out-Null
    }
    catch {
        Write-Host "  /model/pull returned an error (may be non-fatal): $_"
    }

    $MaxWarmupAttempts = 60
    $WarmupSleep       = 15
    $warmedUp          = $false
    for ($w = 1; $w -le $MaxWarmupAttempts; $w++) {
        try {
            $s = Invoke-RestMethod -Uri $StatusUrl -Method Get -TimeoutSec 10 -ErrorAction Stop
            Write-Host "  Model state: $($s.state) (attempt $w/$MaxWarmupAttempts)"
            if ($s.state -eq "ready") { $warmedUp = $true; break }
            if ($s.state -eq "error") {
                $errDetail = if ($s.error) { $s.error } else { "unknown error" }
                Write-Error "Model warm-up failed: $errDetail"
                exit 1
            }
        }
        catch {
            Write-Host "  Attempt $w/$MaxWarmupAttempts — status check failed: $_"
        }
        Start-Sleep -Seconds $WarmupSleep
    }
    if (-not $warmedUp) {
        Write-Warning "Model did not reach 'ready' after $MaxWarmupAttempts attempts. Proceeding anyway..."
    }
}

# ── POST to /generate ─────────────────────────────────────────────────────────
Write-Host "Sending batch to $GenerateUrl ..."

try {
    $resp = Invoke-RestMethod `
        -Uri         $GenerateUrl `
        -Method      Post `
        -ContentType "application/json" `
        -InFile      $BatchFile `
        -TimeoutSec  1800
}
catch {
    Write-Error "Request failed: $_"
    exit 1
}

# ── check top-level status ────────────────────────────────────────────────────
if ($resp.status -eq "error") {
    $errMsg = if ($resp.error) { $resp.error } else { "no error message" }
    Write-Error "Server returned status=error: $errMsg"
    exit 1
}

# ── iterate results ───────────────────────────────────────────────────────────
$saved  = 0
$failed = 0

foreach ($result in $resp.results) {
    if ($result.status -eq "ok" -and $result.image_base64) {
        $absDir = (Resolve-Path -LiteralPath $OutputDir).Path
        $dest   = Join-Path $absDir $result.filename
        $bytes  = [Convert]::FromBase64String($result.image_base64)
        [IO.File]::WriteAllBytes($dest, $bytes)
        Write-Host "Saved $dest"
        $saved++
    }
    else {
        $errMsg = if ($result.error) { $result.error } else { "unknown error" }
        Write-Warning "FAILED: prompt=`"$($result.prompt)`" error=`"$errMsg`""
        $failed++
    }
}

# ── summary ───────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "Summary: $saved saved, $failed failed."

if ($failed -gt 0 -or $resp.status -eq "error") {
    exit 1
}
