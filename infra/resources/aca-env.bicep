param location string = resourceGroup().location
param environmentName string

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
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalyticsWorkspace.properties.customerId
        sharedKey: logAnalyticsWorkspace.listKeys().primarySharedKey
      }
    }
    // Workload profiles required to host containers with > 2 vCPU / 4 Gi (SDXL needs 4 vCPU / 16 Gi).
    // Consumption profile is included alongside dedicated so other apps can still use the env.
    // D4: 4 vCPU / 16 Gi per node; minimumCount 1 keeps the node warm (dedicated cannot scale to 0).
    workloadProfiles: [
      {
        name: 'Consumption'
        workloadProfileType: 'Consumption'
      }
      {
        name: 'dedicated-d4'
        workloadProfileType: 'D4'
        minimumCount: 1
        // maximumCount 2: rolling updates need two nodes briefly — old revision holds
        // the single node while the new revision is being scheduled. With max=1 the new
        // revision can never start (deadlock). Two nodes breaks that deadlock.
        // Live hotfix already applied via az containerapp env workload-profile update.
        maximumCount: 2
      }
    ]
  }
}

output environmentId string = containerAppsEnvironment.id
output environmentName string = containerAppsEnvironment.name
output logAnalyticsWorkspaceId string = logAnalyticsWorkspace.id
