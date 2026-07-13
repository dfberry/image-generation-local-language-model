// Azure Files storage for the HuggingFace model cache.
//
// SDXL weights (~13 GB) are a `from_pretrained` cache — a plain directory tree.
// An Azure File share mounted at /root/.cache/huggingface persists the model
// across revisions and restarts with ZERO app-code changes (blob would need a
// BlobFuse sidecar or custom download logic — ACA has no native blob volume).
//
// The model is loaded lazily on the first /model/pull or /generate, NOT at
// container startup, so Standard_LRS SMB read latency does not affect the
// /health startup probe. Upgrade to a Premium FileStorage account if first-
// inference model-load latency becomes a problem.

param location string = resourceGroup().location
param storageAccountName string
param environmentName string
param fileShareName string = 'models'
param storageName string = 'models-storage'
param shareQuotaGb int = 100
param historyShareName string = 'history'
param historyStorageName string = 'history-storage'
param historyShareQuotaGb int = 50

resource storage 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: storageAccountName
  location: location
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    largeFileSharesState: 'Enabled'
  }
}

resource fileService 'Microsoft.Storage/storageAccounts/fileServices@2023-01-01' = {
  parent: storage
  name: 'default'
}

resource share 'Microsoft.Storage/storageAccounts/fileServices/shares@2023-01-01' = {
  parent: fileService
  name: fileShareName
  properties: {
    shareQuota: shareQuotaGb
    enabledProtocols: 'SMB'
  }
}

resource historyShare 'Microsoft.Storage/storageAccounts/fileServices/shares@2023-01-01' = {
  parent: fileService
  name: historyShareName
  properties: {
    shareQuota: historyShareQuotaGb
    enabledProtocols: 'SMB'
  }
}

// Register the file share with the Container Apps environment under a stable
// name ('models-storage'). The container app's volume references this name.
resource environment 'Microsoft.App/managedEnvironments@2023-05-01' existing = {
  name: environmentName
}

resource environmentStorage 'Microsoft.App/managedEnvironments/storages@2023-05-01' = {
  parent: environment
  name: storageName
  properties: {
    azureFile: {
      accountName: storage.name
      accountKey: storage.listKeys().keys[0].value
      shareName: fileShareName
      accessMode: 'ReadWrite'
    }
  }
  dependsOn: [
    share
  ]
}

resource environmentHistoryStorage 'Microsoft.App/managedEnvironments/storages@2023-05-01' = {
  parent: environment
  name: historyStorageName
  properties: {
    azureFile: {
      accountName: storage.name
      accountKey: storage.listKeys().keys[0].value
      shareName: historyShareName
      accessMode: 'ReadWrite'
    }
  }
  dependsOn: [
    historyShare
  ]
}

output storageAccountName string = storage.name
output storageName string = storageName
output historyStorageName string = historyStorageName
@secure()
output storageAccountKey string = storage.listKeys().keys[0].value
