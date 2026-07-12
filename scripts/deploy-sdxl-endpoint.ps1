# deploy-sdxl-endpoint.ps1 — end-to-end Azure CLI deployment of SDXL as an
# Azure Machine Learning Managed Online Endpoint on managed GPU compute.
#
# Example:
#   $env:RESOURCE_GROUP = "rg-sdxl"; $env:LOCATION = "eastus"; `
#   $env:WORKSPACE = "mlw-sdxl"; .\scripts\deploy-sdxl-endpoint.ps1

param(
    [string]$ResourceGroup = $(if ($env:RESOURCE_GROUP) { $env:RESOURCE_GROUP } else { "rg-sdxl-aml" }),
    [string]$Location = $(if ($env:LOCATION) { $env:LOCATION } else { "eastus" }),
    [string]$Workspace = $(if ($env:WORKSPACE) { $env:WORKSPACE } else { "mlw-sdxl-image-generation" }),
    [string]$EndpointName = $(if ($env:ENDPOINT_NAME) { $env:ENDPOINT_NAME } else { "sdxl-image-endpoint" }),
    [string]$DeploymentName = $(if ($env:DEPLOYMENT_NAME) { $env:DEPLOYMENT_NAME } else { "blue" }),
    [string]$GpuSku = $(if ($env:GPU_SKU) { $env:GPU_SKU } else { "Standard_NC6s_v3" }),
    [int]$InstanceCount = $(if ($env:INSTANCE_COUNT) { [int]$env:INSTANCE_COUNT } else { 1 }),
    [string]$ModelName = $(if ($env:MODEL_NAME) { $env:MODEL_NAME } else { "stabilityai/stable-diffusion-xl-base-1.0" }),
    [string]$ModelVersion = $(if ($env:MODEL_VERSION) { $env:MODEL_VERSION } elseif ($env:MODEL_REVISION) { $env:MODEL_REVISION } else { "main" }),
    [string]$ModelAssetName = $(if ($env:MODEL_ASSET_NAME) { $env:MODEL_ASSET_NAME } else { "sdxl-hf-model" }),
    [string]$ModelAssetVersion = $(if ($env:MODEL_ASSET_VERSION) { $env:MODEL_ASSET_VERSION } else { "1" }),
    [string]$SamplePrompt = $(if ($env:SAMPLE_PROMPT) { $env:SAMPLE_PROMPT } else { "A watercolor painting of a friendly robot painting a sunset over the Pacific Northwest" })
)

$ErrorActionPreference = 'Stop'

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$AssetDir = Join-Path $ScriptDir "sdxl-endpoint"
$ModelDir = Join-Path $AssetDir "model"
$OutputImage = if ($env:OUTPUT_IMAGE) { $env:OUTPUT_IMAGE } else { Join-Path $AssetDir "sample-response.png" }

function Write-Log([string]$Message) {
    Write-Host "sdxl-endpoint: $Message"
}

function Fail([string]$Message) {
    Write-Error $Message
    exit 1
}

function Test-AzCliVersion {
    $minimumVersion = [version]"2.38.0"
    $azVersionOutput = az version -o json | ConvertFrom-Json
    $installedVersion = [version]$azVersionOutput.'azure-cli'
    if ($installedVersion -lt $minimumVersion) {
        Fail "Azure CLI $minimumVersion or later is required for the ml extension. Installed version: $installedVersion. Upgrade with 'az upgrade': https://learn.microsoft.com/cli/azure/install-azure-cli"
    }
}

function Set-GpuQuotaMetadata {
    switch ($GpuSku) {
        "Standard_NC6s_v3" {
            $script:GpuUsageName = "standardNCSv3Family"
            $script:GpuCoresPerInstance = 6
            $script:GpuFamilyLabel = "NCv3 (V100)"
        }
        "Standard_NC24ads_A100_v4" {
            $script:GpuUsageName = "standardNCADSA100v4Family"
            $script:GpuCoresPerInstance = 24
            $script:GpuFamilyLabel = "NC A100 v4"
        }
        "Standard_NC6" {
            Fail "Standard_NC6 is retired. Use Standard_NC6s_v3 or Standard_NC24ads_A100_v4."
        }
        default {
            Fail "Unsupported GPU SKU '$GpuSku'. This script supports Standard_NC6s_v3 and Standard_NC24ads_A100_v4."
        }
    }
}

function Show-QuotaRemediation([int]$Required, [int]$Available) {
    $scope = "/subscriptions/$SubscriptionId/providers/Microsoft.Compute/locations/$Location"
    $limitObject = '{"limit":{"value":' + $Required + ',"limitObjectType":"LimitValue"},"name":{"value":"' + $GpuUsageName + '"},"resourceType":"dedicated"}'
    Write-Error @"
GPU quota is insufficient for $GpuSku ($GpuFamilyLabel) in $Location.
Required dedicated vCPUs: $Required; available dedicated vCPUs: $Available.

Request quota in the same region as the Azure ML workspace before retrying: $Location
AML quota portal:
  https://ml.azure.com/quota

Azure CLI option:
  az extension add --name quota
  az quota create --resource-name "$GpuUsageName" --scope "$scope" --limit-object '$limitObject'

Portal path:
  https://portal.azure.com/#view/Microsoft_Azure_Capacity/QuotaMenuBlade/~/myQuotas
  Filter for region '$Location' and VM family '$GpuFamilyLabel', then request at least $Required dedicated vCPUs.
"@
}

function Test-GpuQuota {
    Set-GpuQuotaMetadata
    $required = $GpuCoresPerInstance * $InstanceCount
    Write-Log "Checking Compute quota for $GpuSku before deployment. Default GPU quota is often 0."

    $usage = az vm list-usage --location $Location `
        --query "[?name.value=='$GpuUsageName'] | [0].{current:currentValue,limit:limit}" `
        -o json | ConvertFrom-Json
    if ($null -eq $usage) {
        Fail "Could not read quota usage '$GpuUsageName' in $Location. Verify the region supports $GpuSku."
    }

    $available = [int]$usage.limit - [int]$usage.current
    if ([int]$usage.limit -eq 0 -or $available -lt $required) {
        Show-QuotaRemediation -Required $required -Available $available
        exit 1
    }
    Write-Log "Quota OK: $available/$($usage.limit) dedicated vCPUs available for $GpuFamilyLabel; $required required."
}

function Write-EndpointAssets {
    New-Item -ItemType Directory -Force -Path $ModelDir | Out-Null

    # Keep endpoint/deployment assets as real files so they can be inspected and
    # committed. The script rewrites the parameterized values on each run.
    @"
`$schema: https://azuremlschemas.azureedge.net/latest/managedOnlineEndpoint.schema.json
name: $EndpointName
auth_mode: key
"@ | Set-Content -Path (Join-Path $AssetDir "endpoint.yml") -Encoding utf8

    @"
`$schema: https://azuremlschemas.azureedge.net/latest/managedOnlineDeployment.schema.json
name: $DeploymentName
endpoint_name: $EndpointName
model:
  name: $ModelAssetName
  version: "$ModelAssetVersion"
  path: ./model
  type: custom_model
environment:
  name: sdxl-diffusers-cuda
  image: mcr.microsoft.com/azureml/curated/acpt-pytorch-2.2-cuda12.1:latest
  conda_file: conda.yaml
code_configuration:
  code: .
  scoring_script: score.py
instance_type: $GpuSku
instance_count: $InstanceCount
environment_variables:
  SDXL_MODEL_NAME: $ModelName
  SDXL_MODEL_REVISION: $ModelVersion
request_settings:
  request_timeout_ms: 180000
  max_concurrent_requests_per_instance: 1
liveness_probe:
  initial_delay: 600
  period: 30
  timeout: 20
  failure_threshold: 10
readiness_probe:
  initial_delay: 600
  period: 30
  timeout: 20
  failure_threshold: 10
"@ | Set-Content -Path (Join-Path $AssetDir "deployment.yml") -Encoding utf8

    @{
        prompt = $SamplePrompt
        negative_prompt = "blurry, low quality, distorted"
        steps = 30
        guidance = 7.5
        width = 1024
        height = 1024
        seed = 12345
    } | ConvertTo-Json | Set-Content -Path (Join-Path $AssetDir "sample-request.json") -Encoding utf8

    @"
# SDXL Hugging Face model marker

This lightweight Azure ML model asset points the deployment at:

- model: ``$ModelName``
- revision: ``$ModelVersion``
- precision: ``fp16``

The weights are resolved by Diffusers at endpoint startup and are not stored in this repository.
"@ | Set-Content -Path (Join-Path $ModelDir "README.md") -Encoding utf8
}

function Wait-ForDeployment {
    $maxAttempts = 80
    $sleepSeconds = 30
    for ($i = 1; $i -le $maxAttempts; $i++) {
        try {
            $state = az ml online-deployment show `
                --name $DeploymentName --endpoint-name $EndpointName `
                --resource-group $ResourceGroup --workspace-name $Workspace `
                --query provisioning_state -o tsv
        }
        catch {
            $state = "Unknown"
        }
        Write-Log "Deployment state: $state (attempt $i/$maxAttempts)"
        if ($state -eq "Succeeded") { return }
        if ($state -eq "Failed" -or $state -eq "Canceled") {
            az ml online-deployment get-logs `
                --name $DeploymentName --endpoint-name $EndpointName `
                --resource-group $ResourceGroup --workspace-name $Workspace `
                --lines 200
            Fail "Deployment provisioning ended in state: $state"
        }
        Start-Sleep -Seconds $sleepSeconds
    }
    az ml online-deployment get-logs `
        --name $DeploymentName --endpoint-name $EndpointName `
        --resource-group $ResourceGroup --workspace-name $Workspace `
        --lines 200
    Fail "Timed out waiting for deployment to reach Succeeded."
}

function Decode-Response {
    $responsePath = Join-Path $AssetDir "sample-response.json"
    $data = Get-Content -Raw -Path $responsePath | ConvertFrom-Json
    if ($data -is [string]) {
        $data = $data | ConvertFrom-Json
    }
    $imageBase64 = if ($data.image_base64) { $data.image_base64 } else { $data.image }
    if (-not $imageBase64) {
        Fail "No image_base64 field found in $responsePath."
    }
    [IO.File]::WriteAllBytes($OutputImage, [Convert]::FromBase64String($imageBase64))
    return $OutputImage
}

# ── 1. Preflight ─────────────────────────────────────────────────────────────
if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
    Fail "Azure CLI 'az' is required: https://learn.microsoft.com/cli/azure/install-azure-cli"
}
Test-AzCliVersion
try {
    $account = az account show -o json | ConvertFrom-Json
}
catch {
    Fail "Run 'az login' and select the target subscription before running this script."
}
if (-not (az extension show --name ml 2>$null)) {
    Write-Log "Azure ML extension missing; installing it."
    az extension add --name ml --only-show-errors
}

$SubscriptionId = $account.id
Write-Log "Target subscription: $($account.name) ($SubscriptionId)"

# ── 2-4. Resource group and workspace ────────────────────────────────────────
if (az group show --name $ResourceGroup 2>$null) {
    Write-Log "Resource group exists: $ResourceGroup"
}
else {
    Write-Log "Creating resource group: $ResourceGroup ($Location)"
    az group create --name $ResourceGroup --location $Location --only-show-errors | Out-Null
}

if (az ml workspace show --name $Workspace --resource-group $ResourceGroup 2>$null) {
    Write-Log "Azure ML workspace exists: $Workspace"
}
else {
    Write-Log "Creating Azure ML workspace: $Workspace"
    az ml workspace create --name $Workspace --resource-group $ResourceGroup --location $Location --only-show-errors | Out-Null
}

# ── 5. GPU quota fail-fast ───────────────────────────────────────────────────
Test-GpuQuota

# ── 6. Generate inspectable endpoint assets ──────────────────────────────────
Write-EndpointAssets
Write-Log "Endpoint assets written to $AssetDir"

# ── 7. Create endpoint and create/update GPU deployment ──────────────────────
Push-Location $AssetDir
try {
    if (az ml online-endpoint show --name $EndpointName --resource-group $ResourceGroup --workspace-name $Workspace 2>$null) {
        Write-Log "Managed online endpoint exists: $EndpointName"
    }
    else {
        Write-Log "Creating managed online endpoint: $EndpointName"
        az ml online-endpoint create --file endpoint.yml --resource-group $ResourceGroup --workspace-name $Workspace
    }

    if (az ml online-deployment show --name $DeploymentName --endpoint-name $EndpointName --resource-group $ResourceGroup --workspace-name $Workspace 2>$null) {
        Write-Log "Updating deployment: $DeploymentName"
        az ml online-deployment update --file deployment.yml --resource-group $ResourceGroup --workspace-name $Workspace
        az ml online-endpoint update --name $EndpointName --traffic "$DeploymentName=100" --resource-group $ResourceGroup --workspace-name $Workspace
    }
    else {
        Write-Log "Creating deployment on $GpuSku; this can take 20-45 minutes while the image builds and SDXL downloads."
        az ml online-deployment create --file deployment.yml --all-traffic --resource-group $ResourceGroup --workspace-name $Workspace
    }
}
finally {
    Pop-Location
}

# ── 8. Poll and dump logs on failure ─────────────────────────────────────────
Wait-ForDeployment

# ── 9. Invoke endpoint and decode returned PNG ───────────────────────────────
$scoringUri = az ml online-endpoint show --name $EndpointName --resource-group $ResourceGroup --workspace-name $Workspace --query scoring_uri -o tsv
$primaryKey = az ml online-endpoint get-credentials --name $EndpointName --resource-group $ResourceGroup --workspace-name $Workspace --query primaryKey -o tsv
Write-Log "Scoring URI: $scoringUri"
Write-Log "Endpoint key retrieved for key-auth invoke (not printed)."

az ml online-endpoint invoke `
    --name $EndpointName `
    --deployment-name $DeploymentName `
    --request-file (Join-Path $AssetDir "sample-request.json") `
    --resource-group $ResourceGroup `
    --workspace-name $Workspace `
    | Set-Content -Path (Join-Path $AssetDir "sample-response.json") -Encoding utf8

$savedPath = Decode-Response
Write-Log "Saved generated image: $savedPath"

# ── 10. Cost teardown hint ───────────────────────────────────────────────────
Write-Host ""
Write-Host "IMPORTANT: Managed GPU endpoints are expensive while running."
Write-Host "Delete the endpoint when finished:"
Write-Host "  az ml online-endpoint delete --name `"$EndpointName`" --resource-group `"$ResourceGroup`" --workspace-name `"$Workspace`" --yes"
Write-Host ""
