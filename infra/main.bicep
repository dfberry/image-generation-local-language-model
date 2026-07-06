param location string = resourceGroup().location
param containerRegistryName string = 'sdxlregistry${uniqueString(resourceGroup().id)}'
param containerAppName string = 'sdxl-generation-api'
param environmentName string = 'sdxl-env'
param containerRegistryUrl string = ''
// azd sets this to true after first provision; allows bicep to read the
// currently-deployed image rather than re-specifying it each run.
// Wired to ${SERVICE_API_RESOURCE_EXISTS} via infra/main.parameters.json —
// without that wiring this stays false forever and the app is stuck running
// the placeholder http.server command instead of the real Flask app.
param apiExists bool = false

// Azure Files storage account for the persistent HuggingFace model cache.
// Globally-unique, deterministic per resource group.
param modelStorageAccountName string = 'sdxlmodels${uniqueString(resourceGroup().id)}'

// Locals
var acrUrl = !empty(containerRegistryUrl) ? containerRegistryUrl : '${containerRegistryName}.azurecr.io'

// User-Assigned Managed Identity — shared by ACR pull role + container app identity
module identity 'resources/uami.bicep' = {
  name: 'identity-deployment'
  params: {
    location: location
    identityName: '${containerAppName}-id'
  }
}

// Container Registry — also creates AcrPull role assignment for the UAMI
module acr 'resources/acr.bicep' = {
  name: 'acr-deployment'
  params: {
    location: location
    containerRegistryName: containerRegistryName
    uamiPrincipalId: identity.outputs.principalId
  }
}

// Container Apps Environment (with Dedicated D4 workload profile)
module caEnvironment 'resources/aca-env.bicep' = {
  name: 'aca-env-deployment'
  params: {
    location: location
    environmentName: environmentName
  }
}

// Azure Files share for the SDXL model cache, registered on the environment
// as 'models-storage'. Created regardless of apiExists so the share is ready
// before the real container (exists=true) mounts it.
module storage 'resources/storage.bicep' = {
  name: 'storage-deployment'
  params: {
    location: location
    storageAccountName: modelStorageAccountName
    environmentName: environmentName
  }
  dependsOn: [
    caEnvironment
  ]
}

// Container App
module containerApp 'resources/aca.bicep' = {
  name: 'container-app-deployment'
  params: {
    location: location
    containerAppName: containerAppName
    containerAppsEnvironmentId: caEnvironment.outputs.environmentId
    containerRegistryUrl: acrUrl
    uamiId: identity.outputs.identityId
    exists: apiExists
    storageAccountName: storage.outputs.storageAccountName
    storageAccountKey: storage.outputs.storageAccountKey
  }
}

// Outputs
// AZURE_CONTAINER_REGISTRY_ENDPOINT: consumed by azd to tag and push the
// service image during `azd deploy`.
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = acr.outputs.loginServer
output containerAppUrl string = containerApp.outputs.fqdn
output containerAppName string = containerApp.outputs.containerAppName
output environmentId string = caEnvironment.outputs.environmentId
output modelStorageAccountName string = storage.outputs.storageAccountName
