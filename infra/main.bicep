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

@description('VNet address prefix - 10.5.0.0/24 = 256 total IP addresses')
param vnetAddressPrefix string = '172.21.1.0/27'

@description('Web App subnet address prefix. 10.5.0.0/27 = 32 IPs (10.5.0.0 - 10.5.0.31)')
param webAppSubnetAddressPrefix string = '172.21.1.0/28'

@description('Private Endpoint subnet address prefix 10.5.0.32/27 = 32 IPs (10.5.0.32 - 10.5.0.63)')
param privateEndpointSubnetAddressPrefix string = '172.21.1.16/29'

@allowed([
  'privateEndpoint'
  'vnetRules'
])
@description('How the Web App connects to Cosmos DB: privateEndpoint (Private Link) or vnetRules (Service Endpoint + VNet firewall rules, no Private Endpoint).')
param cosmosNetworkMode string = 'privateEndpoint'

// Cosmos DB settings
var cosmosDatabaseName = '${environmentName}-database'
var cosmosContainerName = 'Items'
var cosmosContainerMaxThroughput = 1000

// Generate resource names from base name
var resourceGroupName = 'rg-${environmentName}'
var cosmosAccountName = 'cosmos-${environmentName}'
var webAppName = 'app-${environmentName}'
var vnetName = 'vnet-${environmentName}'
var webAppSubnetName = 'snet-webapp'
var privateEndpointSubnetName = 'snet-privateendpoints'
var appServicePlanName = 'asp-${webAppName}'

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
    vnetAddressPrefix: vnetAddressPrefix
    webAppSubnetName: webAppSubnetName
    webAppSubnetAddressPrefix: webAppSubnetAddressPrefix
    privateEndpointSubnetName: privateEndpointSubnetName
    privateEndpointSubnetAddressPrefix: privateEndpointSubnetAddressPrefix
    cosmosNetworkMode: cosmosNetworkMode
    cosmosAccountName: cosmosAccountName
    cosmosDatabaseName: cosmosDatabaseName
    cosmosContainerName: cosmosContainerName
    cosmosContainerMaxThroughput: cosmosContainerMaxThroughput
    webAppName: webAppName
    appServicePlanName: appServicePlanName
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
