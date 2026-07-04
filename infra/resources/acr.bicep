param location string = resourceGroup().location
param containerRegistryName string

resource containerRegistry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: containerRegistryName
  location: location
  sku: {
    name: 'Basic'
  }
  properties: {
    adminUserEnabled: true
    publicNetworkAccess: 'Enabled'
  }
}

output loginServer string = containerRegistry.properties.loginServer
output registryId string = containerRegistry.id
output registryName string = containerRegistry.name
output adminUsername string = containerRegistry.listCredentials().username
output adminPassword string = containerRegistry.listCredentials().passwords[0].value
