targetScope = 'subscription'

@minLength(1)
@maxLength(64)
@description('Name of the environment that can be used as part of naming resource convention')
param environmentName string

@minLength(1)
@description('Primary location for all resources')
param location string

@description('Owner tag for resource tagging')
param owner string = 'defaultuser@example.com'

var tags = {
  'azd-env-name': environmentName
  owner: owner
}

@description('VNet address prefix - 10.5.0.0/24 = 256 total IP addresses. Empty string = use default 172.21.1.0/24.')
param vnetAddressPrefix string = ''

@description('Web App subnet address prefix. 10.5.0.0/27 = 32 IPs (10.5.0.0 - 10.5.0.31). Empty string = use default 172.21.1.0/27.')
param webAppSubnetAddressPrefix string = ''

@description('Private Endpoint subnet address prefix 10.5.0.32/27 = 32 IPs (10.5.0.32 - 10.5.0.63). Empty string = use default 172.21.1.32/27.')
param privateEndpointSubnetAddressPrefix string = ''

@allowed([
  'privateEndpoint'
  'vnetRules'
])
@description('How the Web App connects to Cosmos DB: privateEndpoint (Private Link) or vnetRules (Service Endpoint + VNet firewall rules, no Private Endpoint).')
param cosmosNetworkMode string = 'privateEndpoint'

@description('Secondary location for Cosmos DB replication')
param secondaryLocation string = 'australiasoutheast'

@description('Enable customer-managed keys (CMK) for the Cosmos DB account. Set to "true" to provision a Key Vault + user-assigned identity + key and encrypt Cosmos with it. Empty or any other value = Microsoft-managed keys (default).')
param enableCmk string = ''

// ── Fabric Mirroring over Private Link ──────────────────────────────────────────
// When fabricWorkspaceId is set, the Cosmos account is configured for the
// "Mirroring over Private Link via a Fabric VNet Data Gateway" scenario:
//   - EnableFabricNetworkAclBypass capability
//   - networkAclBypass = AzureServices + the trusted Fabric workspace resource id
//   - a custom mirroring RBAC role (readMetadata/readAnalytics) is defined
// Leave empty (default) to preserve the original harness behavior.
@description('Fabric workspace ID (GUID) to authorize as a trusted workspace for mirroring. Empty = do not configure mirroring network ACL bypass.')
param fabricWorkspaceId string = ''

@description('Fabric tenant ID (GUID) for the trusted workspace resource id. Empty = use the deployment subscription tenant.')
param fabricTenantId string = ''

@description('Object (principal) ID of the Fabric workspace identity to grant Cosmos mirroring RBAC. Empty = skip the role assignment (grant it later via script).')
param fabricWorkspacePrincipalId string = ''

@description('Address prefix for the delegated Fabric VNet Data Gateway subnet (Microsoft.PowerPlatform/vnetaccesslinks). Empty = auto-compute the 5th /27 of the VNet.')
param fabricSubnetAddressPrefix string = ''

// Cosmos DB settings
var cosmosDatabaseName = 'CosmosMirrorDatabase'
var cosmosContainerName = 'Items'
var cosmosContainerMaxThroughput = 1000

// Generate resource names from base name
var resourceGroupName = 'rg-${environmentName}'
var cosmosAccountName = 'cosmos-${environmentName}'
var webAppName = 'app-${environmentName}'
var vnetName = 'vnet-${environmentName}'
var webAppSubnetName = 'snet-webapp'
var privateEndpointSubnetName = 'snet-privateendpoints'
var fabricGatewaySubnetName = 'snet-fabric'
var appServicePlanName = 'asp-${webAppName}'

// Resolve CIDR params: empty string => use defaults
var effectiveVnetAddressPrefix = empty(vnetAddressPrefix) ? '172.21.1.0/24' : vnetAddressPrefix
var effectiveWebAppSubnetAddressPrefix = empty(webAppSubnetAddressPrefix) ? '172.21.1.0/27' : webAppSubnetAddressPrefix
var effectivePrivateEndpointSubnetAddressPrefix = empty(privateEndpointSubnetAddressPrefix) ? '172.21.1.32/27' : privateEndpointSubnetAddressPrefix

// Create resource group
resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: resourceGroupName
  location: location
  tags: tags
}

// Deploy all resources into the resource group via module
module resources './resources.bicep' = {
  name: 'resources-deployment'
  scope: rg
  params: {
    location: location
    vnetName: vnetName
    vnetAddressPrefix: effectiveVnetAddressPrefix
    webAppSubnetName: webAppSubnetName
    webAppSubnetAddressPrefix: effectiveWebAppSubnetAddressPrefix
    privateEndpointSubnetName: privateEndpointSubnetName
    privateEndpointSubnetAddressPrefix: effectivePrivateEndpointSubnetAddressPrefix
    fabricGatewaySubnetName: fabricGatewaySubnetName
    fabricSubnetAddressPrefix: fabricSubnetAddressPrefix
    fabricWorkspaceId: fabricWorkspaceId
    fabricTenantId: fabricTenantId
    fabricWorkspacePrincipalId: fabricWorkspacePrincipalId
    cosmosNetworkMode: cosmosNetworkMode
    cosmosAccountName: cosmosAccountName
    cosmosDatabaseName: cosmosDatabaseName
    cosmosContainerName: cosmosContainerName
    cosmosContainerMaxThroughput: cosmosContainerMaxThroughput
    secondaryLocation: secondaryLocation
    webAppName: webAppName
    appServicePlanName: appServicePlanName
    enableCmk: enableCmk
  }
}

// Outputs
output AZURE_RESOURCE_GROUP string = rg.name
output SERVICE_API_NAME string = resources.outputs.webAppName
output webAppName string = resources.outputs.webAppName
output webAppUrl string = resources.outputs.webAppUrl
output cosmosAccountName string = resources.outputs.cosmosAccountName
output cosmosEndpoint string = resources.outputs.cosmosEndpoint
output vnetName string = resources.outputs.vnetName
output webAppPrincipalId string = resources.outputs.webAppPrincipalId
output cmkEnabled bool = resources.outputs.cmkEnabled
output cmkKeyVaultName string = resources.outputs.cmkKeyVaultName
output cmkKeyUri string = resources.outputs.cmkKeyUri
output mirroringEnabled bool = resources.outputs.mirroringEnabled
output fabricWorkspaceResourceId string = resources.outputs.fabricWorkspaceResourceId
output fabricGatewaySubnetName string = resources.outputs.fabricGatewaySubnetName
