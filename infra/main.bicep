param location string = resourceGroup().location
param containerRegistryName string = 'sdxlregistry${uniqueString(resourceGroup().id)}'
param containerAppName string = 'sdxl-generation-api'
param environmentName string = 'sdxl-env'
param imageName string = ''
param containerRegistryUrl string = ''

// Scaling overrides — can be set at deploy time via azd env set or --parameters.
// minReplicas: 0 = scale-to-zero (cheapest, ~6-min cold start); 1 = always-warm (no cold start, higher cost).
// maxReplicas: keep at 1 to cap cost; raise only if you need concurrent replicas (each adds a full D4 node).
// Typed string (not int) so azd ${ENV_VAR} substitution in parameters.json round-trips cleanly through ARM;
// aca.bicep converts to int with int() before use in the scale block.
param minReplicas string = '0'
param maxReplicas string = '1'

// acrUrl is still used for the registry auth config in aca.bicep.
// SERVICE_API_IMAGE_NAME (injected via parameters.json) is already the full registry/image:tag reference;
// pass it through as-is. When empty (first provision), aca.bicep falls back to the placeholder image.
var acrUrl = !empty(containerRegistryUrl) ? containerRegistryUrl : '${containerRegistryName}.azurecr.io'
var fullImageName = imageName

// Create Container Registry
module acr 'resources/acr.bicep' = {
  name: 'acr-deployment'
  params: {
    location: location
    containerRegistryName: containerRegistryName
  }
}

// Create Azure Files storage account for persistent HF model cache
module storage 'resources/storage.bicep' = {
  name: 'storage-deployment'
  params: {
    location: location
  }
}

// Create Container Apps Environment (wires Azure Files share into the env as 'models-storage')
module caEnvironment 'resources/aca-env.bicep' = {
  name: 'aca-env-deployment'
  params: {
    location: location
    environmentName: environmentName
    storageAccountName: storage.outputs.storageAccountName
    storageAccountKey: storage.outputs.storageAccountKey
    fileShareName: storage.outputs.fileShareName
  }
}

// Create Container App
module containerApp 'resources/aca.bicep' = {
  name: 'container-app-deployment'
  params: {
    location: location
    containerAppName: containerAppName
    containerAppsEnvironmentId: caEnvironment.outputs.environmentId
    imageName: fullImageName
    containerRegistryUrl: acrUrl
    containerRegistryPassword: acr.outputs.adminPassword
    containerRegistryUsername: acr.outputs.adminUsername
    storageAccountName: storage.outputs.storageAccountName
    storageAccountKey: storage.outputs.storageAccountKey
    workloadProfileName: caEnvironment.outputs.workloadProfileName
    minReplicas: minReplicas
    maxReplicas: maxReplicas
  }
}

// Outputs
output containerAppUrl string = containerApp.outputs.fqdn
output containerRegistryUrl string = acr.outputs.loginServer
output containerAppName string = containerApp.outputs.containerAppName
output environmentId string = caEnvironment.outputs.environmentId
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = acr.outputs.loginServer
