#Requires -Modules Az.Accounts
# Create and start a Fabric mirrored database over a private-network Azure Cosmos DB
# account, in a specific workspace, via the Fabric REST API.
# Run Connect-AzAccount first, then:
#   ./create-mirror-rest.ps1 -WorkspaceId <ws> -ConnectionId <conn> -Database <db> -MirrorName <name>
param(
    [Parameter(Mandatory)] [string] $WorkspaceId,   # Fabric workspace GUID
    [Parameter(Mandatory)] [string] $ConnectionId,  # Azure Cosmos DB v2 (VNet gateway) connection id
    [Parameter(Mandatory)] [string] $Database,      # Cosmos database to mirror
    [Parameter(Mandatory)] [string] $MirrorName     # name for the mirrored database
)
$ErrorActionPreference = 'Stop'
$api = 'https://api.fabric.microsoft.com/v1'

$t = Get-AzAccessToken -ResourceUrl 'https://api.fabric.microsoft.com'
$tok = if ($t.Token -is [securestring]) { [System.Net.NetworkCredential]::new('', $t.Token).Password } else { $t.Token }
$h = @{ Authorization = "Bearer $tok"; 'Content-Type' = 'application/json' }
function B64($o) { [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes(($o | ConvertTo-Json -Depth 20))) }

$mirroring = @{ properties = @{
    source = @{ type = 'CosmosDb'; typeProperties = @{ connection = $ConnectionId; database = $Database } }
    target = @{ type = 'MountedRelationalDatabase'; typeProperties = @{ defaultSchema = 'dbo'; format = 'Delta'; retentionInDays = 1; enableDeltaChangeDataFeed = $false } } } }
$platform = @{ '$schema' = 'https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json'
    metadata = @{ type = 'MirroredDatabase'; displayName = $MirrorName }; config = @{ version = '2.0'; logicalId = '00000000-0000-0000-0000-000000000000' } }
$body = @{ displayName = $MirrorName; definition = @{ parts = @(
    @{ path = 'mirroring.json'; payload = (B64 $mirroring); payloadType = 'InlineBase64' }
    @{ path = '.platform';      payload = (B64 $platform);  payloadType = 'InlineBase64' }) } }

$m = Invoke-RestMethod -Method Post -Uri "$api/workspaces/$WorkspaceId/mirroredDatabases" -Headers $h -Body ($body | ConvertTo-Json -Depth 20)
Write-Host "Created mirrored database: $($m.id)"
Invoke-RestMethod -Method Post -Uri "$api/workspaces/$WorkspaceId/mirroredDatabases/$($m.id)/startMirroring" -Headers $h | Out-Null
Write-Host "Mirroring started. Verify with 'Monitor replication' in Fabric."
