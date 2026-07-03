param location string = resourceGroup().location
param containerAppName string
param containerAppsEnvironmentId string
param imageName string
param containerRegistryUrl string
param containerRegistryPassword string = ''
param storageAccountName string = ''
param storageAccountKey string = ''
param fileShareName string = 'huggingface-models'

resource containerApp 'Microsoft.App/containerApps@2023-05-01' = {
  name: containerAppName
  location: location
  properties: {
    environmentId: containerAppsEnvironmentId
    configuration: {
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        allowInsecure: false
        timeout: 120
      }
      registries: !empty(containerRegistryPassword) ? [
        {
          server: containerRegistryUrl
          username: containerRegistryUrl
          passwordSecretRef: 'registry-password'
        }
      ] : []
      secrets: concat(
        !empty(containerRegistryPassword) ? [
          {
            name: 'registry-password'
            value: containerRegistryPassword
          }
        ] : [],
        !empty(storageAccountKey) ? [
          {
            name: 'storage-account-key'
            value: storageAccountKey
          }
        ] : []
      )
      daprConfig: {
        enabled: false
      }
    }
    template: {
      containers: [
        {
          name: 'sdxl-api'
          image: imageName
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
          probes: [
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
          ]
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
        minReplicas: 0
        maxReplicas: 1
      }
    }
  }
}

output fqdn string = containerApp.properties.configuration.ingress.fqdn
output containerAppName string = containerApp.name
output containerAppId string = containerApp.id
