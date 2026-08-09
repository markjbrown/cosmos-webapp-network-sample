#Requires -Modules Az.Accounts, Az.CosmosDB
<#
.SYNOPSIS
    Reset the network ACL / firewall state on a Cosmos DB account (and, optionally,
    named Fabric mirroring artifacts) back to a clean private-link baseline so the
    "Mirroring over Private Link" workflow can be re-run from scratch.

.DESCRIPTION
    SAFE BY DEFAULT: this script performs a DRY RUN and only prints what it WOULD do.
    Pass -Execute to actually apply changes.

    Cosmos-side reset (always in scope when -Execute):
      * Clears the IP firewall rules (the ~400+ DataFactory/PowerQueryOnline IPs left
        over from the older allowlist-based mirroring setup).  <-- "reset the vnet acl"
      * Optionally clears the trusted-workspace network ACL bypass  (-ClearAclBypass).
      * Optionally removes the EnableFabricNetworkAclBypass capability (-RemoveCapability).

    Fabric-side reset is OPT-IN and requires EXPLICIT ids, because the workspace may be
    shared with unrelated production artifacts. Nothing in Fabric is deleted unless you
    pass the specific id(s):
      * -MirrorId <guid>       delete a mirrored database
      * -ConnectionId <guid>   delete a Cosmos DB v2 connection
      * -GatewayId <guid>      delete a VNet Data Gateway

.EXAMPLE
    # Dry run — show what would change:
    .\reset-mirroring-private-link.ps1 -SubscriptionId <sub> -ResourceGroup rg-x -CosmosAccountName cosmos-x

.EXAMPLE
    # Actually clear the leftover IP firewall rules:
    .\reset-mirroring-private-link.ps1 -SubscriptionId <sub> -ResourceGroup rg-x -CosmosAccountName cosmos-x -Execute

.EXAMPLE
    # Full reset including a specific Fabric mirror + connection:
    .\reset-mirroring-private-link.ps1 -SubscriptionId <sub> -ResourceGroup rg-x -CosmosAccountName cosmos-x `
        -ClearAclBypass -RemoveCapability -FabricWorkspaceId <ws> -MirrorId <m> -ConnectionId <c> -Execute
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string] $SubscriptionId,
    [Parameter(Mandatory)] [string] $ResourceGroup,
    [Parameter(Mandatory)] [string] $CosmosAccountName,

    [switch] $ClearAclBypass,
    [switch] $RemoveCapability,

    [string] $FabricWorkspaceId,
    [string] $MirrorId,
    [string] $ConnectionId,
    [string] $GatewayId,

    [switch] $Execute
)

$ErrorActionPreference = 'Stop'
$FabricApi = 'https://api.fabric.microsoft.com/v1'
$mode = if ($Execute) { 'EXECUTE' } else { 'DRY RUN' }
Write-Host "== Cosmos mirroring reset ($mode) ==" -ForegroundColor Cyan
if (-not $Execute) { Write-Host "   No changes will be made. Re-run with -Execute to apply." -ForegroundColor Yellow }

Set-AzContext -Subscription $SubscriptionId | Out-Null

function Do-Step {
    param([string]$Description, [scriptblock]$Action)
    if ($Execute) {
        Write-Host "[apply] $Description" -ForegroundColor Green
        & $Action
    } else {
        Write-Host "[would] $Description" -ForegroundColor Yellow
    }
}

# ── Cosmos-side reset ─────────────────────────────────────────────────────────
$cosmos = Get-AzResource -ResourceGroupName $ResourceGroup -Name $CosmosAccountName -ResourceType 'Microsoft.DocumentDB/databaseAccounts'
$ipCount = @($cosmos.Properties.ipRules).Count
$hasBypass = $cosmos.Properties.networkAclBypass -eq 'AzureServices'
$hasCapability = @($cosmos.Properties.capabilities).name -contains 'EnableFabricNetworkAclBypass'
Write-Host "   Current: publicNetworkAccess=$($cosmos.Properties.publicNetworkAccess), ipRules=$ipCount, networkAclBypass=$($cosmos.Properties.networkAclBypass), EnableFabricNetworkAclBypass=$hasCapability"

if ($ipCount -gt 0) {
    Do-Step "Clear $ipCount IP firewall rule(s) on $CosmosAccountName" {
        Update-AzCosmosDBAccount -ResourceGroupName $ResourceGroup -Name $CosmosAccountName -IpRule @() | Out-Null
    }
} else {
    Write-Host "   No IP firewall rules to clear." -ForegroundColor Gray
}

if ($ClearAclBypass) {
    if ($hasBypass) {
        # The cmdlet's -NetworkAclBypassResourceId rejects an empty array, so set the
        # mode to None here and clear the (often lingering) resource ids via the
        # fresh-object patch below.
        Do-Step "Clear trusted-workspace networkAclBypass on $CosmosAccountName" {
            Update-AzCosmosDBAccount -ResourceGroupName $ResourceGroup -Name $CosmosAccountName -NetworkAclBypass None | Out-Null
        }
    } else {
        Write-Host "   networkAclBypass already None." -ForegroundColor Gray
    }
}

# Capability removal and/or resource-id cleanup share one PATCH. Re-fetch a FRESH
# resource first so the patch does not resend earlier-cleared state (e.g. IP rules).
$needCapRemoval = $RemoveCapability -and $hasCapability
$needIdCleanup = $ClearAclBypass
if ($needCapRemoval -or $needIdCleanup) {
    $desc = if ($needCapRemoval) { "Remove EnableFabricNetworkAclBypass capability + clear bypass resource ids" } else { "Clear networkAclBypass resource ids" }
    Do-Step "$desc on $CosmosAccountName" {
        $fresh = Get-AzResource -ResourceGroupName $ResourceGroup -Name $CosmosAccountName -ResourceType 'Microsoft.DocumentDB/databaseAccounts'
        if ($needCapRemoval) {
            $fresh.Properties.capabilities = @($fresh.Properties.capabilities | Where-Object { $_.name -ne 'EnableFabricNetworkAclBypass' })
        }
        if ($needIdCleanup) {
            $fresh.Properties.networkAclBypassResourceIds = @()
        }
        $fresh | Set-AzResource -UsePatchSemantics -Force | Out-Null
    }
}

# ── Fabric-side reset (opt-in, explicit ids only) ─────────────────────────────
if ($MirrorId -or $ConnectionId -or $GatewayId) {
    $t = Get-AzAccessToken -ResourceUrl 'https://api.fabric.microsoft.com'
    $token = if ($t.Token -is [System.Security.SecureString]) {
        [System.Net.NetworkCredential]::new('', $t.Token).Password
    } else { $t.Token }
    $headers = @{ Authorization = "Bearer $token" }

    if ($MirrorId) {
        if (-not $FabricWorkspaceId) { throw '-FabricWorkspaceId is required to delete a mirrored database.' }
        Do-Step "Delete Fabric mirrored database $MirrorId (workspace $FabricWorkspaceId)" {
            Invoke-RestMethod -Method Delete -Uri "$FabricApi/workspaces/$FabricWorkspaceId/mirroredDatabases/$MirrorId" -Headers $headers | Out-Null
        }
    }
    if ($ConnectionId) {
        Do-Step "Delete Fabric connection $ConnectionId" {
            Invoke-RestMethod -Method Delete -Uri "$FabricApi/connections/$ConnectionId" -Headers $headers | Out-Null
        }
    }
    if ($GatewayId) {
        Do-Step "Delete Fabric VNet Data Gateway $GatewayId" {
            Invoke-RestMethod -Method Delete -Uri "$FabricApi/gateways/$GatewayId" -Headers $headers | Out-Null
        }
    }
} else {
    Write-Host "   (No Fabric artifact ids supplied — skipping Fabric-side reset.)" -ForegroundColor Gray
}

Write-Host ""
Write-Host "Reset $mode complete." -ForegroundColor Cyan
if (-not $Execute) { Write-Host "Re-run with -Execute to apply the changes above." -ForegroundColor Yellow }
