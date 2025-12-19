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

@description('Web App name')
param webAppName string

@description('App Service Plan name')
param appServicePlanName string

var usePrivateEndpoint = cosmosNetworkMode == 'privateEndpoint'
var useVnetRules = cosmosNetworkMode == 'vnetRules'

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
    ]
  }
}

// App Service Plan (Linux, Basic tier)
resource appServicePlan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: appServicePlanName
  location: location
  sku: {
    name: 'B1'
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
  properties: {
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
  }
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
      appCommandLine: 'gunicorn -k uvicorn.workers.UvicornWorker --bind=0.0.0.0:8000 app:app'
      alwaysOn: true
      healthCheckPath: '/'
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

// Outputs
output webAppName string = webApp.name
output webAppUrl string = 'https://${webApp.properties.defaultHostName}'
output cosmosAccountName string = cosmosAccount.name
output cosmosEndpoint string = cosmosAccount.properties.documentEndpoint
output vnetName string = vnet.name
output webAppPrincipalId string = webApp.identity.principalId
