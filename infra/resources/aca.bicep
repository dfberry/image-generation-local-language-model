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
// and pushes during `azd deploy`, which runs AFTER `azd provision`). Use
// python:3.11-slim as placeholder with an explicit command that starts a
// minimal HTTP server on port 8000. This satisfies ACA's default targetPort
// health check (which fires even when probes:[] is set) and prevents the
// crash-loop that deadlocks the rolling update. On subsequent provisions
// (exists=true), read the image the running app is already using so a
// re-provision never clobbers a deployed revision.
resource existingApp 'Microsoft.App/containerApps@2023-05-01' existing = if (exists) {
  name: containerAppName
}

var realImage = exists
  ? (existingApp!.properties.template.containers[0].image ?? 'python:3.11-slim')
  : 'python:3.11-slim'

// Placeholder container: python:3.11-slim + python3 -m http.server 8000.
// No probes — the HTTP server satisfies ACA's default targetPort TCP check.
var placeholderContainer = {
  name: 'sdxl-api'
  image: 'python:3.11-slim'
  command: ['python3', '-m', 'http.server', '8000']
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
  probes: []
}

// Real app container: image read from the running app, explicitly clears the
// placeholder command override so Dockerfile CMD runs, full /health probes.
var realContainer = {
  name: 'sdxl-api'
  image: realImage
  command: []
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
      // Startup probe runs first; gives SDXL (CPU, slow start) time to load.
      // failureThreshold * periodSeconds = 300s window before giving up.
      type: 'Startup'
      httpGet: {
        path: '/health'
        port: 8000
      }
      initialDelaySeconds: 10
      periodSeconds: 10
      failureThreshold: 30
    }
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
      containers: [exists ? realContainer : placeholderContainer]
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
