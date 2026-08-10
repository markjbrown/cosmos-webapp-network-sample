#Requires -Modules Az.Accounts, Az.Resources, Az.CosmosDB
<#
.SYNOPSIS
    Configure Azure Cosmos DB Fabric Mirroring over Private Link using a Fabric
    Virtual Network Data Gateway (no DataFactory/PowerQueryOnline IP allowlists).

.DESCRIPTION
    Automates every step of the "Mirroring over Private Link" workflow that CAN be
    scripted:

      1. Register the Microsoft.PowerPlatform resource provider.
      2. Verify the delegated gateway subnet exists
         (Microsoft.PowerPlatform/vnetaccesslinks).
      3. Enable the EnableFabricNetworkAclBypass capability on the Cosmos account.
      4. Authorize the trusted Fabric workspace (networkAclBypass = AzureServices).
      5. Create (or reuse) the Fabric Virtual Network Data Gateway bound to the subnet.
      6. Create and start the mirrored database via the Fabric REST API.

    The ONE step that cannot be automated is creating the Azure Cosmos DB v2
    connection, because it requires an interactive OAuth 2.0 sign-in in the Fabric
    portal. Create that connection once (bound to the gateway from step 5), then pass
    its id via -ConnectionId, or its name via -ConnectionName for auto-discovery.

    Steps 3, 4 and the delegated subnet can also be provisioned declaratively by the
    repo's Bicep (see infra/resources.bicep, fabricWorkspaceId parameter). This script
    is idempotent, so running it against a Bicep-provisioned account is safe.

.NOTES
    Auth: uses your current Az PowerShell context (run Connect-AzAccount first).
          The same identity must be an Admin on the target Fabric workspace and have
          Contributor + RBAC-assignment rights on the Cosmos account.
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory)] [string] $SubscriptionId,
    [Parameter(Mandatory)] [string] $ResourceGroup,
    [Parameter(Mandatory)] [string] $CosmosAccountName,
    [Parameter(Mandatory)] [string] $VNetName,
    [string] $FabricSubnetName = 'snet-fabric',

    [Parameter(Mandatory)] [string] $FabricWorkspaceId,
    [string] $FabricTenantId,

    # VNet Data Gateway
    [string] $GatewayName,
    [string] $CapacityId,
    [ValidateRange(1, 7)] [int] $NumberOfMemberGateways = 1,
    [int] $InactivityMinutesBeforeSleep = 30,

    # Mirror
    [Parameter(Mandatory)] [string] $CosmosDatabaseName,
    [Parameter(Mandatory)] [string] $MirrorName,

    # The Cosmos DB v2 (OAuth) connection. Provide the id, or a name to discover.
    [string] $ConnectionId,
    [string] $ConnectionName
)

$ErrorActionPreference = 'Stop'
$FabricApi = 'https://api.fabric.microsoft.com/v1'

function Get-FabricHeaders {
    $t = Get-AzAccessToken -ResourceUrl 'https://api.fabric.microsoft.com'
    $token = if ($t.Token -is [System.Security.SecureString]) {
        [System.Net.NetworkCredential]::new('', $t.Token).Password
    } else { $t.Token }
    return @{ Authorization = "Bearer $token"; 'Content-Type' = 'application/json' }
}

function Invoke-Fabric {
    param([string]$Method, [string]$Path, $Body)
    $headers = Get-FabricHeaders
    $uri = "$FabricApi$Path"
    if ($Body) {
        return Invoke-RestMethod -Method $Method -Uri $uri -Headers $headers -Body ($Body | ConvertTo-Json -Depth 20)
    }
    return Invoke-RestMethod -Method $Method -Uri $uri -Headers $headers
}

Write-Host "== Setting subscription context ==" -ForegroundColor Cyan
Set-AzContext -Subscription $SubscriptionId | Out-Null
if (-not $FabricTenantId) { $FabricTenantId = (Get-AzContext).Tenant.Id }
if (-not $GatewayName) { $GatewayName = "$VNetName-$FabricSubnetName" }

# 1. Register the Microsoft.PowerPlatform resource provider ---------------------
Write-Host "== [1/6] Registering Microsoft.PowerPlatform ==" -ForegroundColor Cyan
$rp = Get-AzResourceProvider -ProviderNamespace Microsoft.PowerPlatform
if ($rp.RegistrationState -notcontains 'Registered') {
    if ($PSCmdlet.ShouldProcess('Microsoft.PowerPlatform', 'Register resource provider')) {
        Register-AzResourceProvider -ProviderNamespace Microsoft.PowerPlatform | Out-Null
        Write-Host "   Registration submitted (can take a few minutes)." -ForegroundColor Yellow
    }
} else {
    Write-Host "   Already registered." -ForegroundColor Green
}

# 2. Verify the delegated gateway subnet ---------------------------------------
Write-Host "== [2/6] Verifying delegated subnet '$FabricSubnetName' ==" -ForegroundColor Cyan
$vnet = Get-AzVirtualNetwork -ResourceGroupName $ResourceGroup -Name $VNetName
$subnet = $vnet.Subnets | Where-Object Name -eq $FabricSubnetName
if (-not $subnet) { throw "Subnet '$FabricSubnetName' not found in VNet '$VNetName'. Provision it (delegated to Microsoft.PowerPlatform/vnetaccesslinks) via Bicep first." }
$delegation = $subnet.Delegations | Where-Object { $_.ServiceName -eq 'Microsoft.PowerPlatform/vnetaccesslinks' }
if (-not $delegation) { throw "Subnet '$FabricSubnetName' is not delegated to Microsoft.PowerPlatform/vnetaccesslinks." }
Write-Host "   OK ($($subnet.AddressPrefix))." -ForegroundColor Green

# 3. Enable EnableFabricNetworkAclBypass ---------------------------------------
Write-Host "== [3/6] Enabling EnableFabricNetworkAclBypass capability ==" -ForegroundColor Cyan
$cosmos = Get-AzResource -ResourceGroupName $ResourceGroup -Name $CosmosAccountName -ResourceType 'Microsoft.DocumentDB/databaseAccounts'
$capabilities = @($cosmos.Properties.capabilities)
if ($capabilities.name -notcontains 'EnableFabricNetworkAclBypass') {
    if ($PSCmdlet.ShouldProcess($CosmosAccountName, 'Add EnableFabricNetworkAclBypass capability')) {
        $cosmos.Properties.capabilities = $capabilities + @{ name = 'EnableFabricNetworkAclBypass' }
        $cosmos | Set-AzResource -UsePatchSemantics -Force | Out-Null
        Write-Host "   Capability added." -ForegroundColor Green
    }
} else {
    Write-Host "   Already enabled." -ForegroundColor Green
}

# 4. Authorize the trusted Fabric workspace ------------------------------------
Write-Host "== [4/6] Authorizing trusted Fabric workspace ==" -ForegroundColor Cyan
$workspaceResourceId = "/tenants/$FabricTenantId/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/Fabric/providers/Microsoft.Fabric/workspaces/$FabricWorkspaceId"
if ($PSCmdlet.ShouldProcess($CosmosAccountName, "Set networkAclBypass -> $workspaceResourceId")) {
    Update-AzCosmosDBAccount -ResourceGroupName $ResourceGroup -Name $CosmosAccountName `
        -NetworkAclBypass AzureServices -NetworkAclBypassResourceId $workspaceResourceId | Out-Null
    Write-Host "   Trusted workspace authorized." -ForegroundColor Green
}

# 5. Create (or reuse) the Fabric Virtual Network Data Gateway -----------------
Write-Host "== [5/6] Ensuring Fabric VNet Data Gateway '$GatewayName' ==" -ForegroundColor Cyan
$gateways = (Invoke-Fabric -Method GET -Path '/gateways').value
$gateway = $gateways | Where-Object { $_.type -eq 'VirtualNetwork' -and $_.displayName -eq $GatewayName }
if (-not $gateway) {
    if (-not $CapacityId) {
        $ws = Invoke-Fabric -Method GET -Path "/workspaces/$FabricWorkspaceId"
        $CapacityId = $ws.capacityId
    }
    if (-not $CapacityId) { throw "CapacityId is required to create a gateway. Pass -CapacityId or ensure the workspace is assigned to a capacity." }
    $body = @{
        type                         = 'VirtualNetwork'
        displayName                  = $GatewayName
        capacityId                   = $CapacityId
        inactivityMinutesBeforeSleep = $InactivityMinutesBeforeSleep
        numberOfMemberGateways       = $NumberOfMemberGateways
        virtualNetworkAzureResource  = @{
            subscriptionId    = $SubscriptionId
            resourceGroupName = $ResourceGroup
            virtualNetworkName = $VNetName
            subnetName        = $FabricSubnetName
        }
    }
    if ($PSCmdlet.ShouldProcess($GatewayName, 'Create VNet Data Gateway')) {
        $gateway = Invoke-Fabric -Method POST -Path '/gateways' -Body $body
        Write-Host "   Gateway created: $($gateway.id)" -ForegroundColor Green
    }
} else {
    Write-Host "   Reusing gateway: $($gateway.id)" -ForegroundColor Green
}

# --- MANUAL GATE: Azure Cosmos DB v2 (OAuth 2.0) connection -------------------
if (-not $ConnectionId) {
    if ($ConnectionName) {
        $conn = (Invoke-Fabric -Method GET -Path '/connections').value | Where-Object displayName -eq $ConnectionName
        if ($conn) { $ConnectionId = $conn.id }
    }
}
if (-not $ConnectionId) {
    Write-Host ""
    Write-Host "ACTION REQUIRED — create the Azure Cosmos DB v2 connection (one-time, manual):" -ForegroundColor Yellow
    Write-Host "  1. Fabric portal > Settings > Manage connections and gateways > Connections > + New."
    Write-Host "  2. Connection type: Azure Cosmos DB v2.  Connectivity: Virtual Network."
    Write-Host "     Gateway: $GatewayName"
    Write-Host "  3. Authentication kind: OAuth 2.0 / Organizational account."
    Write-Host "     Endpoint: https://$CosmosAccountName.documents.azure.com:443/"
    Write-Host "  4. Test connection, then Create."
    Write-Host ""
    Write-Host "OAuth cannot be scripted. Re-run with -ConnectionId <id> (or -ConnectionName <name>) once created." -ForegroundColor Yellow
    return
}
Write-Host "   Using Cosmos DB v2 connection: $ConnectionId" -ForegroundColor Green

# 6. Create and start the mirrored database ------------------------------------
Write-Host "== [6/6] Creating mirrored database '$MirrorName' ==" -ForegroundColor Cyan
function ConvertTo-B64Json($obj) {
    return [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes(($obj | ConvertTo-Json -Depth 20)))
}
$mirroringJson = @{
    properties = @{
        source = @{ type = 'CosmosDb'; typeProperties = @{ connection = $ConnectionId; database = $CosmosDatabaseName } }
        target = @{ type = 'MountedRelationalDatabase'; typeProperties = @{ defaultSchema = 'dbo'; format = 'Delta'; retentionInDays = 1; enableDeltaChangeDataFeed = $false } }
    }
}
$platform = @{
    '$schema' = 'https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json'
    metadata  = @{ type = 'MirroredDatabase'; displayName = $MirrorName }
    config    = @{ version = '2.0'; logicalId = '00000000-0000-0000-0000-000000000000' }
}
$createBody = @{
    displayName = $MirrorName
    definition  = @{
        parts = @(
            @{ path = 'mirroring.json'; payload = (ConvertTo-B64Json $mirroringJson); payloadType = 'InlineBase64' }
            @{ path = '.platform'; payload = (ConvertTo-B64Json $platform); payloadType = 'InlineBase64' }
        )
    }
}
if ($PSCmdlet.ShouldProcess($MirrorName, 'Create + start mirrored database')) {
    $mirror = Invoke-Fabric -Method POST -Path "/workspaces/$FabricWorkspaceId/mirroredDatabases" -Body $createBody
    Write-Host "   Mirrored database created: $($mirror.id)" -ForegroundColor Green
    try {
        Invoke-Fabric -Method POST -Path "/workspaces/$FabricWorkspaceId/mirroredDatabases/$($mirror.id)/startMirroring" | Out-Null
        Write-Host "   Mirroring started." -ForegroundColor Green
    } catch {
        Write-Host "   (startMirroring returned: $($_.Exception.Message) — it may already be running.)" -ForegroundColor Yellow
    }
    try {
        $status = Invoke-Fabric -Method POST -Path "/workspaces/$FabricWorkspaceId/mirroredDatabases/$($mirror.id)/getMirroringStatus"
        Write-Host "   Status: $($status.status)" -ForegroundColor Green
    } catch { }
}

Write-Host ""
Write-Host "Done. Verify replication in the Fabric portal (Monitor replication)." -ForegroundColor Cyan
