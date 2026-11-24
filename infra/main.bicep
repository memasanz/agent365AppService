targetScope = 'resourceGroup'

param location string = resourceGroup().location

@minLength(2)
@maxLength(60)
param webAppName string
param planName string = '${webAppName}-plan'
param managedIdentityName string = '${webAppName}-blueprint-mi'

@allowed(['B1', 'S1', 'P1v3'])
param sku string = 'B1'

@description('Explicit IPv4/IPv6 CIDRs permitted to call the app. No rules means deny all. This is not user authentication.')
param callerCidrs array = []

@description('Separate explicit deployment-machine CIDRs for the SCM endpoint; never inherited from the app allowlist.')
param scmCallerCidrs array = []

@description('Existing attached UAMI resource IDs, obtained by deploy.ps1 to avoid detaching unrelated identities.')
param existingUserAssignedIdentityIds array = []

var retainedIdentities = toObject(existingUserAssignedIdentityIds, id => toLower(id), id => {})

resource telemetryIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: managedIdentityName
  location: location
  tags: {
    agent365StarterWebAppResourceId: resourceId('Microsoft.Web/sites', webAppName)
  }
}

resource plan 'Microsoft.Web/serverfarms@2025-03-01' = {
  name: planName
  location: location
  kind: 'linux'
  sku: {
    name: sku
    capacity: 1
  }
  properties: {
    reserved: true
  }
}

// Keep SystemAssigned enabled on every update: disabling/recreating it loses its role bindings.
// App settings are deliberately merged separately by the scripts, not replaced by ARM.
resource webApp 'Microsoft.Web/sites@2025-03-01' = {
  name: webAppName
  location: location
  kind: 'app,linux'
  identity: {
    type: 'SystemAssigned, UserAssigned'
    userAssignedIdentities: union(retainedIdentities, {
      '${toLower(telemetryIdentity.id)}': {}
    })
  }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    publicNetworkAccess: 'Enabled'
    clientAffinityEnabled: false
    siteConfig: {
      linuxFxVersion: 'PYTHON|3.12'
      appCommandLine: 'python -m uvicorn src.app:app --host 0.0.0.0 --port 8000'
      alwaysOn: true
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      scmMinTlsVersion: '1.2'
      http20Enabled: true
      remoteDebuggingEnabled: false
      ipSecurityRestrictionsDefaultAction: 'Deny'
      ipSecurityRestrictions: [for (cidr, index) in callerCidrs: {
        name: 'caller-${index}'
        ipAddress: cidr
        action: 'Allow'
        priority: 100 + index
        tag: 'Default'
      }]
      scmIpSecurityRestrictionsUseMain: false
      scmIpSecurityRestrictionsDefaultAction: 'Deny'
      scmIpSecurityRestrictions: [for (cidr, index) in scmCallerCidrs: {
        name: 'deployment-${index}'
        ipAddress: cidr
        action: 'Allow'
        priority: 100 + index
        tag: 'Default'
      }]
    }
  }
}

resource scmBasicAuth 'Microsoft.Web/sites/basicPublishingCredentialsPolicies@2025-03-01' = {
  parent: webApp
  name: 'scm'
  properties: {
    allow: false
  }
}

resource ftpBasicAuth 'Microsoft.Web/sites/basicPublishingCredentialsPolicies@2025-03-01' = {
  parent: webApp
  name: 'ftp'
  properties: {
    allow: false
  }
}

output tenantId string = tenant().tenantId
output subscriptionId string = subscription().subscriptionId
output resourceGroup string = resourceGroup().name
output webAppName string = webApp.name
output webAppResourceId string = webApp.id
output hostname string = webApp.properties.defaultHostName
output systemAssignedPrincipalId string = webApp.identity.principalId
output managedIdentityResourceId string = telemetryIdentity.id
output managedIdentityClientId string = telemetryIdentity.properties.clientId
output managedIdentityPrincipalId string = telemetryIdentity.properties.principalId
