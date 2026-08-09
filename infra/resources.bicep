targetScope = 'resourceGroup'

@description('Primary location for all resources')
param location string

@description('VNet name')
param vnetName string

@description('VNet address prefix')
param vnetAddressPrefix string

@description('Web App subnet name')
param webAppSubnetName string

@description('Web App subnet address prefix')
param webAppSubnetAddressPrefix string

@description('Private Endpoint subnet name')
param privateEndpointSubnetName string

@description('Private Endpoint subnet address prefix')
param privateEndpointSubnetAddressPrefix string

@description('Fabric VNet Data Gateway subnet name')
param fabricGatewaySubnetName string = 'snet-fabric'

@description('Fabric VNet Data Gateway subnet address prefix. Empty = auto-compute the 5th /27 of the VNet.')
param fabricSubnetAddressPrefix string = ''

@description('Fabric workspace ID (GUID) to authorize as a trusted workspace. Empty = do not configure mirroring network ACL bypass.')
param fabricWorkspaceId string = ''

@description('Fabric tenant ID (GUID). Empty = use the deployment subscription tenant.')
param fabricTenantId string = ''

@description('Object (principal) ID of the Fabric workspace identity to grant Cosmos mirroring RBAC. Empty = skip the assignment.')
param fabricWorkspacePrincipalId string = ''

@allowed([
  'privateEndpoint'
  'vnetRules'
])
@description('How the Web App connects to Cosmos DB: privateEndpoint (Private Link) or vnetRules (Service Endpoint + VNet firewall rules, no Private Endpoint).')
param cosmosNetworkMode string = 'privateEndpoint'

@description('Cosmos DB account name')
param cosmosAccountName string

@description('Cosmos DB database name')
param cosmosDatabaseName string

@description('Cosmos DB container name')
param cosmosContainerName string

@description('Cosmos DB container max throughput')
param cosmosContainerMaxThroughput int

@description('Secondary location for Cosmos DB replication')
param secondaryLocation string

@description('Web App name')
param webAppName string

@description('App Service Plan name')
param appServicePlanName string

@description('Enable customer-managed keys (CMK) for Cosmos. "true" provisions Key Vault + user-assigned identity + key and encrypts the account. Empty/other = Microsoft-managed keys.')
param enableCmk string = ''

var usePrivateEndpoint = cosmosNetworkMode == 'privateEndpoint'
var useVnetRules = cosmosNetworkMode == 'vnetRules'
var cmkEnabled = enableCmk == 'true'

// ── Fabric Mirroring over Private Link ──────────────────────────────────────────
// Configured only when a Fabric workspace ID is supplied. This wires the Cosmos
// account for the "VNet Data Gateway" mirroring path (no DataFactory/PowerQuery IP
// allowlists): a trusted-workspace network ACL bypass + a delegated gateway subnet.
var mirroringEnabled = !empty(fabricWorkspaceId)
var effectiveFabricTenantId = empty(fabricTenantId) ? subscription().tenantId : fabricTenantId
// The Fabric workspace resource id is a synthetic id under a fixed placeholder
// subscription/resource group, per the Fabric mirroring documentation.
var fabricWorkspaceResourceId = '/tenants/${effectiveFabricTenantId}/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/Fabric/providers/Microsoft.Fabric/workspaces/${fabricWorkspaceId}'
// Default the gateway subnet to the 5th /27 of the VNet (index 4) so it doesn't
// collide with the web app (index 0) or private endpoint (index 1) subnets.
var effectiveFabricSubnetAddressPrefix = empty(fabricSubnetAddressPrefix) ? cidrSubnet(vnetAddressPrefix, 27, 4) : fabricSubnetAddressPrefix

// ── Customer-Managed Keys (CMK) scaffolding ─────────────────────────────────────
// Only provisioned when cmkEnabled. A user-assigned managed identity is granted
// wrap/unwrap on a Key Vault key, and the Cosmos account is created with that key
// as its encryption key (defaultIdentity points at the UAMI). CMK must be set at
// account creation time — it cannot be added to an existing Cosmos account.
var cmkIdentityName = 'id-cmk-${cosmosAccountName}'
var keyVaultName = 'kv-${uniqueString(resourceGroup().id, cosmosAccountName)}'
var cmkKeyName = 'cosmos-cmk-key'

resource cmkIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = if (cmkEnabled) {
  name: cmkIdentityName
  location: location
}

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = if (cmkEnabled) {
  name: keyVaultName
  location: location
  properties: {
    sku: {
      family: 'A'
      name: 'standard'
    }
    tenantId: subscription().tenantId
    // Soft delete + purge protection are REQUIRED for Cosmos DB CMK.
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
    enablePurgeProtection: true
    // Access-policy model (not RBAC) so the UAMI grant applies immediately at
    // vault creation, before the Cosmos account references the key.
    enableRbacAuthorization: false
    accessPolicies: [
      {
        tenantId: subscription().tenantId
        objectId: cmkEnabled ? cmkIdentity.properties.principalId : ''
        permissions: {
          keys: [
            'get'
            'wrapKey'
            'unwrapKey'
          ]
        }
      }
    ]
    publicNetworkAccess: 'Enabled'
  }
}

resource cmkKey 'Microsoft.KeyVault/vaults/keys@2023-07-01' = if (cmkEnabled) {
  parent: keyVault
  name: cmkKeyName
  properties: {
    kty: 'RSA'
    keySize: 3072
    keyOps: [
      'wrapKey'
      'unwrapKey'
    ]
  }
}

// Versionless key URI enables automatic key-version rotation for Cosmos.
var cmkKeyUri = cmkEnabled ? '${keyVault.properties.vaultUri}keys/${cmkKeyName}' : ''

// Virtual Network
resource vnet 'Microsoft.Network/virtualNetworks@2024-01-01' = {
  name: vnetName
  location: location
  properties: {
    addressSpace: {
      addressPrefixes: [
        vnetAddressPrefix
      ]
    }
    subnets: [
      {
        name: webAppSubnetName
        properties: {
          addressPrefix: webAppSubnetAddressPrefix
          delegations: [
            {
              name: 'delegation'
              properties: {
                serviceName: 'Microsoft.Web/serverFarms'
              }
            }
          ]
          serviceEndpoints: useVnetRules ? [
            {
              service: 'Microsoft.AzureCosmosDB'
            }
          ] : []
          privateEndpointNetworkPolicies: 'Enabled'
        }
      }
      {
        name: privateEndpointSubnetName
        properties: {
          addressPrefix: privateEndpointSubnetAddressPrefix
          privateEndpointNetworkPolicies: 'Disabled'
        }
      }
      {
        // Dedicated, empty subnet delegated to Power Platform for the Fabric
        // Virtual Network Data Gateway used by Cosmos DB mirroring over private link.
        name: fabricGatewaySubnetName
        properties: {
          addressPrefix: effectiveFabricSubnetAddressPrefix
          delegations: [
            {
              name: 'delegation'
              properties: {
                serviceName: 'Microsoft.PowerPlatform/vnetaccesslinks'
              }
            }
          ]
          privateEndpointNetworkPolicies: 'Disabled'
        }
      }
    ]
  }
}

// App Service Plan (Linux, Basic tier)
resource appServicePlan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: appServicePlanName
  location: location
  sku: {
    name: 'B3'
    tier: 'Basic'
  }
  properties: {
    reserved: true // Linux
  }
}

// Cosmos DB Account
resource cosmosAccount 'Microsoft.DocumentDB/databaseAccounts@2024-05-15' = {
  name: cosmosAccountName
  location: location
  kind: 'GlobalDocumentDB'
  identity: cmkEnabled ? {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${cmkIdentity.id}': {}
    }
  } : {
    type: 'None'
  }
  properties: union({
    databaseAccountOfferType: 'Standard'
    disableLocalAuth: true
    consistencyPolicy: {
      defaultConsistencyLevel: 'Session'
    }
    locations: [
      {
        locationName: location
        failoverPriority: 0
        isZoneRedundant: false
      }
      {
        locationName: secondaryLocation
        failoverPriority: 1
        isZoneRedundant: false
      }
    ]
    publicNetworkAccess: usePrivateEndpoint ? 'Disabled' : 'Enabled'
    isVirtualNetworkFilterEnabled: useVnetRules
    virtualNetworkRules: useVnetRules ? [
      {
        id: resourceId('Microsoft.Network/virtualNetworks/subnets', vnetName, webAppSubnetName)
        ignoreMissingVNetServiceEndpoint: false
      }
    ] : []
    enableAutomaticFailover: false
    enableMultipleWriteLocations: false
    backupPolicy: {
      type: 'Continuous'
      continuousModeProperties: {
        tier: 'Continuous7Days'
      }
    }
  }, cmkEnabled ? {
    // Encrypt the account with the customer-managed key, accessed via the UAMI.
    keyVaultKeyUri: cmkKeyUri
    defaultIdentity: 'UserAssignedIdentity=${cmkIdentity.id}'
  } : {}, mirroringEnabled ? {
    // Trusted Fabric workspace network ACL bypass — lets the authorized Fabric
    // workspace reach the account even with public access disabled, without the
    // DataFactory/PowerQueryOnline IP allowlist required by the older approach.
    capabilities: [
      {
        name: 'EnableFabricNetworkAclBypass'
      }
    ]
    networkAclBypass: 'AzureServices'
    networkAclBypassResourceIds: [
      fabricWorkspaceResourceId
    ]
  } : {})
  dependsOn: cmkEnabled ? [
    cmkKey
  ] : []
}

// Cosmos DB Database
resource cosmosDatabase 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2024-05-15' = {
  parent: cosmosAccount
  name: cosmosDatabaseName
  properties: {
    resource: {
      id: cosmosDatabaseName
    }
  }
}

// Cosmos DB Container with autoscale
resource cosmosContainer 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-05-15' = {
  parent: cosmosDatabase
  name: cosmosContainerName
  properties: {
    resource: {
      id: cosmosContainerName
      partitionKey: {
        paths: [
          '/id'
        ]
        kind: 'Hash'
      }
      indexingPolicy: {
        indexingMode: 'consistent'
        automatic: true
        includedPaths: [
          {
            path: '/*'
          }
        ]
        excludedPaths: [
          {
            path: '/"_etag"/?'
          }
        ]
      }
    }
    options: {
      autoscaleSettings: {
        maxThroughput: cosmosContainerMaxThroughput
      }
    }
  }
}

// Private DNS Zone for Cosmos DB
resource privateDnsZone 'Microsoft.Network/privateDnsZones@2024-06-01' = if (usePrivateEndpoint) {
  name: 'privatelink.documents.azure.com'
  location: 'global'
}

// Link Private DNS Zone to VNet
resource privateDnsZoneLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = if (usePrivateEndpoint) {
  parent: privateDnsZone
  name: '${vnetName}-link'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: vnet.id
    }
  }
}

// Private Endpoint for Cosmos DB
resource cosmosPrivateEndpoint 'Microsoft.Network/privateEndpoints@2024-01-01' = if (usePrivateEndpoint) {
  name: 'pe-${cosmosAccountName}'
  location: location
  properties: {
    subnet: {
      id: '${vnet.id}/subnets/${privateEndpointSubnetName}'
    }
    privateLinkServiceConnections: [
      {
        name: 'cosmos-connection'
        properties: {
          privateLinkServiceId: cosmosAccount.id
          groupIds: [
            'Sql'
          ]
        }
      }
    ]
  }
}

// Private DNS Zone Group for Cosmos DB Private Endpoint
resource cosmosPeDnsGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-01-01' = if (usePrivateEndpoint) {
  parent: cosmosPrivateEndpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'config'
        properties: {
          privateDnsZoneId: privateDnsZone.id
        }
      }
    ]
  }
}

// Web App
resource webApp 'Microsoft.Web/sites@2023-12-01' = {
  name: webAppName
  location: location
  kind: 'app,linux'
  tags: {
    'azd-service-name': 'api'
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    serverFarmId: appServicePlan.id
    reserved: true
    virtualNetworkSubnetId: '${vnet.id}/subnets/${webAppSubnetName}'
    // NOTE: Leaving this disabled avoids accidental egress breakage (e.g., remote build / package downloads)
    // when the VNet has no NAT configured. Enable only if you intentionally want all outbound traffic routed via VNet.
    vnetRouteAllEnabled: false
    httpsOnly: true
    siteConfig: {
      linuxFxVersion: 'PYTHON|3.11'
      appCommandLine: 'startup.sh'
      alwaysOn: true
      healthCheckPath: '/api/health'
      appSettings: [
        {
          name: 'COSMOS_ENDPOINT'
          value: cosmosAccount.properties.documentEndpoint
        }
        {
          name: 'COSMOS_DATABASE_NAME'
          value: cosmosDatabaseName
        }
        {
          name: 'COSMOS_CONTAINER_NAME'
          value: cosmosContainerName
        }
        {
          name: 'PORT'
          value: '8000'
        }
        {
          name: 'AZURE_SUBSCRIPTION_ID'
          value: subscription().subscriptionId
        }
        {
          name: 'AZURE_RESOURCE_GROUP'
          value: resourceGroup().name
        }
        {
          name: 'COSMOS_ACCOUNT_NAME'
          value: cosmosAccountName
        }
        {
          name: 'SCM_DO_BUILD_DURING_DEPLOYMENT'
          value: 'true'
        }
      ]
      cors: {
        allowedOrigins: [
          'https://portal.azure.com'
        ]
      }
    }
  }
  dependsOn: usePrivateEndpoint ? [
    cosmosPeDnsGroup
  ] : []
}

// Assign Cosmos DB Data Contributor role to Web App
var cosmosDataContributorRoleId = '00000000-0000-0000-0000-000000000002'
resource roleAssignment 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-05-15' = {
  parent: cosmosAccount
  name: guid(cosmosAccount.id, webApp.id, cosmosDataContributorRoleId)
  properties: {
    roleDefinitionId: '${cosmosAccount.id}/sqlRoleDefinitions/${cosmosDataContributorRoleId}'
    principalId: webApp.identity.principalId
    scope: cosmosAccount.id
  }
}

// Assign Cosmos DB Operator role to Web App (allows failover operations)
var cosmosDbOperatorRoleId = '230815da-be43-4aae-9cb4-875f7bd000aa'
resource failoverRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(cosmosAccount.id, webApp.id, cosmosDbOperatorRoleId)
  scope: cosmosAccount
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', cosmosDbOperatorRoleId)
    principalId: webApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// ── Fabric Mirroring RBAC ───────────────────────────────────────────────────────
// Custom data-plane role granting the metadata/analytics read permissions Fabric
// mirroring needs. Assigned to the Fabric workspace identity (when its principal id
// is supplied), along with the Built-in Data Contributor role.
resource fabricMirroringRoleDef 'Microsoft.DocumentDB/databaseAccounts/sqlRoleDefinitions@2024-05-15' = if (mirroringEnabled) {
  parent: cosmosAccount
  name: guid(cosmosAccount.id, 'FabricMirroringMetadataReader')
  properties: {
    roleName: 'Fabric Mirroring Metadata Reader'
    type: 'CustomRole'
    assignableScopes: [
      cosmosAccount.id
    ]
    permissions: [
      {
        dataActions: [
          'Microsoft.DocumentDB/databaseAccounts/readMetadata'
          'Microsoft.DocumentDB/databaseAccounts/readAnalytics'
        ]
      }
    ]
  }
}

resource fabricMirroringRoleAssignment 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-05-15' = if (mirroringEnabled && !empty(fabricWorkspacePrincipalId)) {
  parent: cosmosAccount
  name: guid(cosmosAccount.id, fabricWorkspacePrincipalId, 'FabricMirroringMetadataReader')
  properties: {
    roleDefinitionId: fabricMirroringRoleDef.id
    principalId: fabricWorkspacePrincipalId
    scope: cosmosAccount.id
  }
}

resource fabricDataContributorAssignment 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-05-15' = if (mirroringEnabled && !empty(fabricWorkspacePrincipalId)) {
  parent: cosmosAccount
  name: guid(cosmosAccount.id, fabricWorkspacePrincipalId, cosmosDataContributorRoleId)
  properties: {
    roleDefinitionId: '${cosmosAccount.id}/sqlRoleDefinitions/${cosmosDataContributorRoleId}'
    principalId: fabricWorkspacePrincipalId
    scope: cosmosAccount.id
  }
}

// Outputs
output webAppName string = webApp.name
output webAppUrl string = 'https://${webApp.properties.defaultHostName}'
output cosmosAccountName string = cosmosAccount.name
output cosmosEndpoint string = cosmosAccount.properties.documentEndpoint
output vnetName string = vnet.name
output webAppPrincipalId string = webApp.identity.principalId
output cmkEnabled bool = cmkEnabled
output cmkKeyVaultName string = cmkEnabled ? keyVault.name : ''
output cmkKeyUri string = cmkKeyUri
output mirroringEnabled bool = mirroringEnabled
output fabricWorkspaceResourceId string = mirroringEnabled ? fabricWorkspaceResourceId : ''
output fabricGatewaySubnetName string = fabricGatewaySubnetName
