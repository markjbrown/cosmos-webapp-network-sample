#!/usr/bin/env bash
# Create and start a Fabric mirrored database over a private-network Azure Cosmos DB
# account, in a specific workspace, via the Fabric REST API.
# Requires az (logged in via `az login`), curl, jq. Runs in Azure Cloud Shell (Bash).
#   ./create-mirror-rest.sh <workspace-id> <connection-id> <database> <mirror-name>
set -euo pipefail

WS="$1"; CONN="$2"; DB="$3"; NAME="$4"
API="https://api.fabric.microsoft.com/v1"
TOKEN="$(az account get-access-token --resource https://api.fabric.microsoft.com --query accessToken -o tsv)"

MIRRORING=$(jq -cn --arg c "$CONN" --arg d "$DB" '{properties:{source:{type:"CosmosDb",typeProperties:{connection:$c,database:$d}},target:{type:"MountedRelationalDatabase",typeProperties:{defaultSchema:"dbo",format:"Delta",retentionInDays:1,enableDeltaChangeDataFeed:false}}}}' | base64 -w 0)
PLATFORM=$(jq -cn --arg n "$NAME" '{"$schema":"https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",metadata:{type:"MirroredDatabase",displayName:$n},config:{version:"2.0",logicalId:"00000000-0000-0000-0000-000000000000"}}' | base64 -w 0)
BODY=$(jq -cn --arg n "$NAME" --arg m "$MIRRORING" --arg p "$PLATFORM" '{displayName:$n,definition:{parts:[{path:"mirroring.json",payload:$m,payloadType:"InlineBase64"},{path:".platform",payload:$p,payloadType:"InlineBase64"}]}}')

ID=$(curl -sS -X POST "$API/workspaces/$WS/mirroredDatabases" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d "$BODY" | jq -r '.id')
echo "Created mirrored database: $ID"
curl -sS -X POST "$API/workspaces/$WS/mirroredDatabases/$ID/startMirroring" -H "Authorization: Bearer $TOKEN" >/dev/null
echo "Mirroring started. Verify with 'Monitor replication' in Fabric."
