param location string = resourceGroup().location
param environmentName string
param storageAccountName string = ''
@secure()
param storageAccountKey string = ''
param fileShareName string = 'huggingface-models'

resource logAnalyticsWorkspace 'Microsoft.OperationalInsights/workspaces@2021-12-01-preview' = {
  name: '${environmentName}-logs'
  location: location
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
  }
}

resource containerAppsEnvironment 'Microsoft.App/managedEnvironments@2023-05-01' = {
  name: environmentName
  location: location
  properties: {
    // Dedicated D4 profile (4 vCPU / 16 Gi) for SDXL CPU inference; minimumCount 0 = scale-to-zero.
    // Consumption profile retained so non-SDXL apps can use the same env cheaply.
    workloadProfiles: [
      {
        name: 'Consumption'
        workloadProfileType: 'Consumption'
      }
      {
        name: 'sdxl-profile'
        workloadProfileType: 'D4'
        minimumCount: 0
        maximumCount: 1
      }
    ]
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalyticsWorkspace.properties.customerId
        sharedKey: logAnalyticsWorkspace.listKeys().primarySharedKey
      }
    }
  }
}

// Bind the Azure Files share to the environment so aca.bicep's storageName: 'models-storage' resolves correctly.
// Conditioned on !empty so the module stays usable without storage in dev/test scenarios.
resource envStorage 'Microsoft.App/managedEnvironments/storages@2023-05-01' = if (!empty(storageAccountName)) {
  parent: containerAppsEnvironment
  name: 'models-storage'
  properties: {
    azureFile: {
      accountName: storageAccountName
      accountKey: storageAccountKey
      shareName: fileShareName
      accessMode: 'ReadWrite'
    }
  }
}

output environmentId string = containerAppsEnvironment.id
output environmentName string = containerAppsEnvironment.name
output logAnalyticsWorkspaceId string = logAnalyticsWorkspace.id
output workloadProfileName string = 'sdxl-profile'
