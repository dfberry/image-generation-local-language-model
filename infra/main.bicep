param location string = resourceGroup().location
param containerRegistryName string = 'sdxlregistry${uniqueString(resourceGroup().id)}'
param containerAppName string = 'sdxl-generation-api'
param environmentName string = 'sdxl-env'
param imageName string = 'sdxl-api:latest'
param containerRegistryUrl string = ''

// Locals
var acrUrl = !empty(containerRegistryUrl) ? containerRegistryUrl : '${containerRegistryName}.azurecr.io'
var fullImageName = '${acrUrl}/${imageName}'

// Create Container Registry
module acr 'resources/acr.bicep' = {
  name: 'acr-deployment'
  params: {
    location: location
    containerRegistryName: containerRegistryName
  }
}

// Create Container Apps Environment
module caEnvironment 'resources/aca-env.bicep' = {
  name: 'aca-env-deployment'
  params: {
    location: location
    environmentName: environmentName
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
    containerRegistryPassword: ''
  }
}

// Outputs
output containerAppUrl string = containerApp.outputs.fqdn
output containerRegistryUrl string = acr.outputs.loginServer
output containerAppName string = containerApp.outputs.containerAppName
output environmentId string = caEnvironment.outputs.environmentId
