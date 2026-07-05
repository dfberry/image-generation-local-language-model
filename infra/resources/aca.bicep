param location string = resourceGroup().location
param containerAppName string
param containerAppsEnvironmentId string
param imageName string = ''
param containerRegistryUrl string
@secure()
param containerRegistryPassword string = ''
param containerRegistryUsername string = ''
param storageAccountName string = ''
@secure()
param storageAccountKey string = ''
param workloadProfileName string = 'sdxl-profile'

// minReplicas=0 → scale-to-zero: near-$0 idle, ~6-min cold start on first request after idle (cheapest).
// minReplicas=1 → always warm: model stays in memory, no cold start, but the replica runs 24/7 (higher cost).
// Typed string so azd ${ENV_VAR} substitution in parameters.json flows through ARM without a type mismatch;
// int() converts to int at the point of use in the scale block below.
param minReplicas string = '0'

// maxReplicas MUST stay at 1 to cap cost. SDXL is single-request CPU inference; more replicas = more D4 nodes.
param maxReplicas string = '1'

resource containerApp 'Microsoft.App/containerApps@2023-05-01' = {
  name: containerAppName
  location: location
  tags: { 'azd-service-name': 'api' }
  properties: {
    workloadProfileName: workloadProfileName
    environmentId: containerAppsEnvironmentId
    configuration: {
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        allowInsecure: false
      }
      registries: !empty(containerRegistryPassword) ? [
        {
          server: containerRegistryUrl
          username: containerRegistryUsername
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
      dapr: {
        enabled: false
      }
    }
    template: {
      containers: [
        {
          name: 'sdxl-api'
          image: !empty(imageName) ? imageName : 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'
          resources: {
            // D4 (4 vCPU / 16 Gi) is the smallest dedicated ACA SKU that fits SDXL.
            // The Consumption profile is cheaper but caps at 8 Gi — SDXL will very likely OOM there.
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
        minReplicas: int(minReplicas)
        maxReplicas: int(maxReplicas)
      }
    }
  }
}

output fqdn string = containerApp.properties.configuration.ingress.fqdn
output containerAppName string = containerApp.name
output containerAppId string = containerApp.id
