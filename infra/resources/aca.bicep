param location string = resourceGroup().location
param containerAppName string
param containerAppsEnvironmentId string
param containerRegistryUrl string
param uamiId string                    // Resource ID of the user-assigned managed identity
param exists bool = false              // Set by azd: true after first provision; false on first run
param storageAccountName string = ''
@secure()
param storageAccountKey string = ''
param workloadProfileName string = 'dedicated-d4'

// On first provision the real image hasn't been pushed to ACR yet (azd builds
// and pushes during `azd deploy`, which runs AFTER `azd provision`). Use a
// public Microsoft placeholder so the container app provisions without
// authentication or a missing-tag error. azd deploy then calls
// `az containerapp update --image <real>` and the probes are live after that.
//
// On subsequent provisions (exists=true), read the image the running app is
// already using so a re-provision never clobbers a deployed revision.
resource existingApp 'Microsoft.App/containerApps@2023-05-01' existing = if (exists) {
  name: containerAppName
}

var containerImage = exists
  ? (existingApp.properties.template.containers[0].image ?? 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest')
  : 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'

resource containerApp 'Microsoft.App/containerApps@2023-05-01' = {
  name: containerAppName
  location: location
  // azd uses this tag to locate the container app for `azd deploy` updates
  tags: {
    'azd-service-name': 'api'
  }
  // UAMI gives the container app permission to pull from ACR at runtime
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${uamiId}': {}
    }
  }
  properties: {
    environmentId: containerAppsEnvironmentId
    workloadProfileName: workloadProfileName
    configuration: {
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        allowInsecure: false
      }
      // UAMI-based registry auth: no password secret required
      registries: [
        {
          server: containerRegistryUrl
          identity: uamiId
        }
      ]
      secrets: !empty(storageAccountKey) ? [
        {
          name: 'storage-account-key'
          value: storageAccountKey
        }
      ] : []
    }
    template: {
      containers: [
        {
          name: 'sdxl-api'
          image: containerImage
          resources: {
            cpu: json('4')
            memory: '16Gi'
          }
          env: [
            {
              name: 'PORT'
              value: '8000'
            }
          ]
          volumeMounts: !empty(storageAccountName) ? [
            {
              volumeName: 'models-cache'
              mountPath: '/root/.cache/huggingface'
            }
          ] : []
          // Health probes only active once the real image is deployed.
          // The placeholder image does not serve /health on port 8000.
          probes: exists ? [
            {
              type: 'Liveness'
              httpGet: {
                path: '/health'
                port: 8000
              }
              initialDelaySeconds: 60
              periodSeconds: 30
            }
            {
              type: 'Readiness'
              httpGet: {
                path: '/health'
                port: 8000
              }
              initialDelaySeconds: 30
              periodSeconds: 10
            }
          ] : []
        }
      ]
      volumes: !empty(storageAccountName) ? [
        {
          name: 'models-cache'
          storageType: 'AzureFile'
          storageName: 'models-storage'
        }
      ] : []
      scale: {
        // Dedicated workload profiles do not support scale-to-zero; minimum is 1
        minReplicas: 1
        maxReplicas: 1
      }
    }
  }
}

output fqdn string = containerApp.properties.configuration.ingress.fqdn
output containerAppName string = containerApp.name
output containerAppId string = containerApp.id
