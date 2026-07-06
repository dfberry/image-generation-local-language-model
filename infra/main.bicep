param location string = resourceGroup().location
param containerRegistryName string = 'sdxlregistry${uniqueString(resourceGroup().id)}'
param containerAppName string = 'sdxl-generation-api'
param environmentName string = 'sdxl-env'
param containerRegistryUrl string = ''
// azd sets this to true after first provision; allows bicep to read the
// currently-deployed image rather than re-specifying it each run.
param apiExists bool = false

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
  }
}

// Outputs
// AZURE_CONTAINER_REGISTRY_ENDPOINT: consumed by azd to tag and push the
// service image during `azd deploy`.
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = acr.outputs.loginServer
output containerAppUrl string = containerApp.outputs.fqdn
output containerAppName string = containerApp.outputs.containerAppName
output environmentId string = caEnvironment.outputs.environmentId
