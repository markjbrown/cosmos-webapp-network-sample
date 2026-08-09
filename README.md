# Cosmos DB in a Private Network with Fabric Mirroring — sample harness

A runnable Azure sample that shows you, end to end, how to put **Azure Cosmos DB behind a private network** and **mirror it into Microsoft Fabric** — the way most enterprise customers actually want to deploy it.

`azd up` provisions:

- A **Virtual Network** with two subnets (web app + private endpoints).
- An **Azure Cosmos DB** account with public access disabled and reachable only from inside the VNet — either through a **Private Endpoint + Private DNS** or through **Service Endpoint + VNet firewall rules** (your choice, one parameter).
- A **Python (FastAPI) web app** on App Service, VNet-integrated, that talks to Cosmos using its **Managed Identity** — no connection strings.
- A small **single-page UI** (and Swagger UI at `/docs`) that lets you exercise the account, watch the wiring work, and turn on **Fabric Mirroring** against it.

It's deliberately small and readable: one `app.py`, one HTML page, two Bicep files. The point is to make a private-network + Mirroring topology something you can stand up, poke at, tear down, and copy into your own infra without guessing.

## What's in the box

| Resource | Notes |
|---|---|
| Resource group | Tagged with `azd-env-name` and `owner` |
| Virtual Network | `/24` with two `/27` subnets (web app + private endpoints). CIDR auto-selected to avoid overlap with VNets you already own (see below). |
| Cosmos DB account | Two regions (single write region), continuous backup (7 days), local auth disabled |
| App Service Plan | Linux, **B3 (Basic)** — sized for evaluation, not production |
| Web App | Python 3.11, VNet-integrated, system-assigned Managed Identity |
| Private DNS zone | `privatelink.documents.azure.com` (privateEndpoint mode only) |
| Cosmos Private Endpoint | privateEndpoint mode only |
| RBAC role assignments | `Cosmos DB Built-in Data Contributor` (data plane) + `Cosmos DB Operator` (control plane) on the web app's MI |

The Fabric Mirrored Database itself is **not** provisioned by Bicep — it's created from the deployed app's setup card (or by you in the Fabric portal). That keeps the harness from needing Fabric admin permissions at deploy time.

## Prerequisites

- [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli)
- [Azure Developer CLI (`azd`)](https://learn.microsoft.com/azure/developer/azure-developer-cli/install-azd)
- An Azure subscription with permission to create a resource group, VNet, Cosmos account, and App Service Plan
- **Python 3** on your local machine (the preprovision hook uses it to auto-pick a non-overlapping VNet CIDR — see below)
- A [Microsoft Fabric](https://learn.microsoft.com/fabric/) workspace if you want to enable Mirroring (optional — the private-network deployment alone works without Fabric)

## Quick start

```bash
git clone <this-repo>
cd cosmos-webapp-network-sample
az login
azd auth login
azd up
```

`azd up` will prompt for:

- An **environment name** (used as the resource group name and resource prefix)
- A **subscription**
- A **location** (default `westcentralus`)
- The **Cosmos network mode** (`privateEndpoint` or `vnetRules`) — pick one and stick with it for the lifetime of the environment

When provisioning finishes, `azd` prints the Web App URL. Open it. The first card walks you through enabling Fabric Mirroring against the Cosmos account that was just deployed.

## VNet address planning — handled for you

The biggest source of friction with private-network deployments on busy subscriptions is finding a `/24` that doesn't already overlap with another VNet. The `azd up` preprovision hook calls `tools/ip_planner.py`, which:

1. Lists every existing VNet address space in your current subscription (`az network vnet list`).
2. Walks the `172.16.0.0/16` range looking for the first non-overlapping `/24`.
3. Carves it into two `/27` subnets (web app + private endpoints).
4. Sets the result as `azd` env variables, which are passed straight into Bicep.

You'll see a line like:

```
Auto-planned VNet CIDR: 172.16.7.0/24 (webapp 172.16.7.0/27, private endpoints 172.16.7.32/27)
```

If you want to **pin** a CIDR (e.g. you have a fixed IP allocation from your network team):

```bash
azd env set VNET_ADDRESS_PREFIX 10.50.4.0/24
azd env set WEBAPP_SUBNET_ADDRESS_PREFIX 10.50.4.0/27
azd env set PRIVATE_ENDPOINT_SUBNET_ADDRESS_PREFIX 10.50.4.32/27
azd up
```

The hook respects an existing `VNET_ADDRESS_PREFIX` and skips planning. To re-plan: `azd env set VNET_ADDRESS_PREFIX ""` and `azd up` again.

You can also run the planner standalone and inspect what it would pick:

```bash
python tools/ip_planner.py --format json
python tools/ip_planner.py --webapp-ips 12 --cosmos-ips 4   # right-size the subnets
```

See [tools/README.md](tools/README.md) for all options.

## Setting up Fabric Mirroring

Open the **Fabric Mirror Setup** card in the deployed app:

1. Sign in to your Fabric tenant (uses your own access — no service principal setup needed).
2. The card creates a Mirrored Database in your chosen workspace, pointing at the Cosmos account this harness deployed.
3. The workspace + mirrored DB name are saved in your browser's local storage so the verification flows can find them later.

You can also configure Mirroring manually in the Fabric portal and just type the workspace + mirrored DB name into the setup card — the harness uses the SQL Analytics endpoint of the mirrored DB to query.

### Mirroring over Private Link (no IP allowlists)

When Cosmos has **public network access disabled**, you can mirror into Fabric **without**
maintaining the large DataFactory/PowerQueryOnline IP allowlists by using a **Fabric
Virtual Network Data Gateway** plus a trusted-workspace network ACL bypass. This repo
automates everything except the one interactive **OAuth** step:

- **Bicep** (`infra/resources.bicep`, set `FABRIC_WORKSPACE_ID`) provisions the
  `EnableFabricNetworkAclBypass` capability, the trusted-workspace bypass, the custom
  mirroring RBAC role, and the delegated `snet-fabric` gateway subnet.
- **`tools/setup-mirroring-private-link.ps1`** registers the Power Platform RP, creates
  the VNet Data Gateway, and creates + starts the mirror via the Fabric REST API.
- **`tools/reset-mirroring-private-link.ps1`** resets the account's network ACL (and
  optional named Fabric artifacts) to a clean baseline — dry-run by default.

See **[docs/mirroring-over-private-link.md](docs/mirroring-over-private-link.md)** for the
full step-by-step guide and automation breakdown.

## Switching Cosmos network mode

```bash
azd env set COSMOS_NETWORK_MODE vnetRules   # or privateEndpoint
azd up
```

Set this **before** running `azd up`. ARM incremental deployments will not delete an existing Private Endpoint when you flip back to `vnetRules` — use a new `azd` environment name, or `azd down --force --purge` and redeploy.

## Faster deploys

App Service Python deployments do a remote build (`pip install`) by default, which is slow. To build a deterministic zip locally and deploy that:

```powershell
pwsh -File .\tools\build_local_package.ps1
```

The web app is also configured with **Always On** and a `/api/health` health check path for stable startup.

## Verification helpers

```bash
# Get the web app URL
azd env get-values | grep webAppUrl

# Confirm VNet integration
RG_NAME=$(azd env get-values | grep AZURE_RESOURCE_GROUP | cut -d'=' -f2)
WEBAPP_NAME=$(azd env get-values | grep webAppName | cut -d'=' -f2)
az webapp vnet-integration list --name $WEBAPP_NAME --resource-group $RG_NAME

# Tail web app logs
az webapp log tail --name $WEBAPP_NAME --resource-group $RG_NAME
```

> **Note on the Cosmos Data Explorer:** in `privateEndpoint` mode, the portal Data Explorer cannot reach the account because `publicNetworkAccess` is disabled. This is expected. Use the harness's own query endpoint, or the **Try it out** flow in `/docs`.

## Cleanup

```bash
azd down --force --purge
```

This removes everything the harness created — including the Cosmos account and any data in it. It does **not** remove a Mirrored Database you created in your Fabric workspace; delete that manually from the Fabric portal.

## Troubleshooting

**Deployment fails with `The address space ... is already in use`**
Re-run `azd env set VNET_ADDRESS_PREFIX ""` and `azd up` to force the planner to pick a fresh range. If the planner can't find one, scope a wider search range with `python tools/ip_planner.py --base 10.0.0.0/8 --format json` and pin the result.

**Web app can't connect to Cosmos**

1. Confirm VNet integration is active: `az webapp vnet-integration list ...`
2. In `privateEndpoint` mode, confirm the Private DNS zone is linked to the VNet
3. Confirm the web app's MI has the `Cosmos DB Built-in Data Contributor` data-plane role
4. Tail logs: `az webapp log tail ...`

**Fabric verification fails or hangs**

- Confirm you signed into the **Fabric Mirror Setup** card with an account that has access to your workspace.
- Confirm the Mirrored Database is in a healthy state in the Fabric portal (not still initializing).
- The SQL Analytics endpoint can lag behind Cosmos by a few seconds — the verification flows already wait up to 5 minutes for catch-up.

---

# Bonus: regional failover resilience testing

The harness includes a **Run Full Test** card that exercises a much more demanding scenario than the basic Mirroring setup: it triggers a **manual regional failover of Cosmos DB while writes are in flight** and reports back on what survived. This is included because "what actually happens to in-flight writes during a failover, and does Mirroring stay consistent?" is one of the most common questions we get asked, and pictures beat docs.

The flow:

1. Start a long-running insert loop into Cosmos.
2. Trigger a **manual regional failover** mid-batch.
3. Continue inserting through the failover.
4. Wait for Mirroring to catch up.
5. Verify that every successful Cosmos commit reached Fabric.

It then renders one of three verdicts — with **explicit, separate judgments** for *Cosmos Mirroring* and for *your application's resilience*:

| Verdict | Meaning | What to do |
|---|---|---|
| 🟢 **PASS** | Every insert succeeded **and** every Cosmos commit reached Fabric. | Nothing — celebrate. |
| 🟠 **PARTIAL** | Cosmos Mirroring passed (zero silent data loss), but the application lost in-flight writes during the failover window. | This is the **most common** outcome and reveals an *application gap*, not a Mirroring gap. See below. |
| 🔴 **FAIL** | Successfully committed Cosmos writes are **missing** from Fabric. | This is a Mirroring problem. Capture the run details and report it. |

### Interpreting PARTIAL — and why it matters

A PARTIAL verdict means two things at once:

1. **Cosmos Mirroring did its job perfectly.** Every write that Cosmos persisted made it to Fabric. There is no silent data loss.
2. **Your application code lost writes that never made it into Cosmos in the first place.** The SDK call hung past the harness's per-request timeout (30 s) during the failover, and the harness has no retry policy to re-attempt the writes that timed out.

This is exactly what a typical customer application looks like by default. The Cosmos SDK does not transparently retry a write across a manual regional failover with out-of-the-box settings, and most apps don't add their own retry policy on top.

**To survive a planned failover without losing writes, your application needs:**

- An explicit **retry policy** for transient errors (`CosmosHttpResponseError 503`, `asyncio.TimeoutError`, `ServiceRequestError`, etc.). Use exponential backoff with jitter, capped at a sensible maximum.
- A **request timeout** sized to your account's failover RPO/RTO, not the Python async default (which has no default).
- Optionally, an explicit **`preferred_locations`** configuration on the `CosmosClient` so the SDK already knows where to look when the primary write endpoint becomes unreachable.

A polished sample of a resilient client is a planned addition. The goal of this section as published today is to *demonstrate* the gap so you can decide how to close it in your own application.

### Running the failover test

In the **Run Full Test** card, set:

- **Total items** — how many inserts to run (300 is a good baseline)
- **Delay between inserts** — milliseconds between each insert (100 ms is realistic for a steady workload)
- **Trigger failover after N items** — when to flip the primary region mid-batch (40 is a good default)
- **Failover to region** — picked from the account's secondary region(s)

Hit **Run Full Test**. The app executes all 6 steps and renders a verdict. Steps stream live so you can watch the failover happen.

Note: a manual failover takes a Cosmos account out of write availability for ~30-60 s. Don't run this against an account anyone else is using.

## Repository layout

```
.
├── app.py                  # FastAPI app (insert loop, failover trigger, Mirror verification)
├── static/index.html       # Single-page UI for the harness
├── startup.sh              # App Service startup (installs ODBC Driver 18, runs gunicorn)
├── requirements.txt
├── docs/
│   └── mirroring-over-private-link.md  # VNet Data Gateway mirroring guide (no IP allowlists)
├── infra/
│   ├── main.bicep          # Subscription-scope entrypoint + parameters
│   ├── main.parameters.json
│   └── resources.bicep     # All resource definitions
├── azure.yaml              # azd config + preprovision hook (CIDR planner, OWNER_EMAIL, COSMOS_NETWORK_MODE)
└── tools/
    ├── ip_planner.py                       # Pick a non-overlapping VNet CIDR for your subscription
    ├── build_local_package.ps1             # Build a zip with deps for faster deploys
    ├── setup-mirroring-private-link.ps1    # Configure trust + gateway + start mirror (REST)
    └── reset-mirroring-private-link.ps1    # Reset network ACL / Fabric artifacts (dry-run default)
```

