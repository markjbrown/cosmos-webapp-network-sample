import asyncio
import json
import os
import re
import struct
import time
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

import requests as http_requests
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field
from azure.cosmos import CosmosClient
from azure.cosmos.aio import CosmosClient as AsyncCosmosClient
from azure.identity import DefaultAzureCredential, DeviceCodeCredential
from azure.identity.aio import DefaultAzureCredential as AsyncDefaultAzureCredential
from azure.mgmt.cosmosdb import CosmosDBManagementClient
import threading
import time

# pyodbc is optional — only needed for Fabric SQL endpoints.
# It requires the ODBC Driver 18 native library to be installed on the host.
try:
    import pyodbc
    pyodbc.pooling = False
    _pyodbc_available = True
except ImportError:
    _pyodbc_available = False

app = FastAPI(
    title="Cosmos DB Mirror Failover Test API",
    description="API for testing Cosmos DB regional failover with Fabric mirroring verification.",
    version="2.0.0",
)

# ── Configuration ──────────────────────────────────────────────────────────────
cosmos_endpoint = os.environ.get('COSMOS_ENDPOINT')
database_name = os.environ.get('COSMOS_DATABASE_NAME')
container_name = os.environ.get('COSMOS_CONTAINER_NAME')

# Management API config
subscription_id = os.environ.get('AZURE_SUBSCRIPTION_ID')
resource_group = os.environ.get('AZURE_RESOURCE_GROUP')
cosmos_account_name = os.environ.get('COSMOS_ACCOUNT_NAME')

# Shared credentials (singletons)
_credential = DefaultAzureCredential()
_async_credential: AsyncDefaultAzureCredential | None = None
_async_cosmos_client: AsyncCosmosClient | None = None
_async_cosmos_container = None

# In-memory batch progress tracker (single-worker assumption)
_batch_progress: dict[str, dict] = {}
# Strong references so background tasks aren't GC'd mid-flight
_running_tasks: set = set()
# Per-batch cancellation flags
_batch_cancel: dict[str, bool] = {}


def _get_token_for(scope: str) -> str:
    """Acquire an access token using the app's managed identity / DefaultAzureCredential."""
    return _credential.get_token(scope).token


# ── User Device-Code Auth (lets the user sign in instead of relying on the MI) ─
# Public Azure CLI client ID — no app registration needed.
_AZ_CLI_CLIENT_ID = "04b07795-8ddb-461a-bbee-02f9e1bf7b46"
_user_credential: DeviceCodeCredential | None = None
_user_signed_in: bool = False
_user_upn: str | None = None
# A pasted SQL token from the user (for tenants with Conditional Access that block device-code)
_user_pasted_sql_token: str | None = None
_user_pasted_sql_token_exp: int | None = None  # epoch seconds
_last_token_source: str = "managed_identity"  # one of: header, pasted, device_code, managed_identity
_device_flow_state: dict = {
    "user_code": None,
    "verification_uri": None,
    "message": None,
    "expires_at": None,
    "in_progress": False,
    "error": None,
}
_device_flow_lock = threading.Lock()


def _device_prompt_callback(verification_uri: str, user_code: str, expires_on):
    with _device_flow_lock:
        _device_flow_state["verification_uri"] = verification_uri
        _device_flow_state["user_code"] = user_code
        _device_flow_state["message"] = (
            f"Open {verification_uri} and enter code {user_code}"
        )
        _device_flow_state["expires_at"] = (
            expires_on.isoformat() if hasattr(expires_on, "isoformat") else str(expires_on)
        )


def _start_device_flow_thread(tenant_id: str | None):
    """Background thread: kicks off DeviceCodeCredential and acquires SQL token."""
    global _user_credential, _user_signed_in, _user_upn
    try:
        cred = DeviceCodeCredential(
            client_id=_AZ_CLI_CLIENT_ID,
            tenant_id=tenant_id or "common",
            prompt_callback=_device_prompt_callback,
            timeout=600,
        )
        # This blocks until the user enters the code in the browser.
        token = cred.get_token("https://database.windows.net/.default")
        _user_credential = cred
        _user_signed_in = True
        # Decode JWT to get UPN (best-effort)
        try:
            import base64
            payload = token.token.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            claims = json.loads(base64.urlsafe_b64decode(payload))
            _user_upn = claims.get("upn") or claims.get("preferred_username") or claims.get("unique_name")
        except Exception:
            _user_upn = None
        with _device_flow_lock:
            _device_flow_state["in_progress"] = False
            _device_flow_state["user_code"] = None
            _device_flow_state["verification_uri"] = None
            _device_flow_state["message"] = None
            _device_flow_state["error"] = None
    except Exception as exc:
        with _device_flow_lock:
            _device_flow_state["in_progress"] = False
            _device_flow_state["error"] = str(exc)


def _resolve_token(provided: str | None, scope: str) -> str:
    """Use the provided header token, then user-pasted SQL token, then user device-code credential, then MI."""
    global _last_token_source
    if provided:
        _last_token_source = "header"
        return provided
    # User-pasted SQL token (only valid for the SQL/database scope)
    if (
        scope == "https://database.windows.net/.default"
        and _user_pasted_sql_token
        and _user_pasted_sql_token_exp
        and _user_pasted_sql_token_exp > int(time.time()) + 30
    ):
        _last_token_source = "pasted"
        return _user_pasted_sql_token
    # Prefer the user's own credential if signed in
    if _user_credential is not None and _user_signed_in:
        try:
            tok = _user_credential.get_token(scope).token
            _last_token_source = "device_code"
            return tok
        except Exception:
            # Fall through to MI on user-credential failure
            pass
    _last_token_source = "managed_identity"
    try:
        return _get_token_for(scope)
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=(
                f"Failed to acquire token for {scope} via managed identity: {e}. "
                "Ensure the app's identity has been granted access to the resource, "
                "or pass a token explicitly via the header, or sign in via the dashboard."
            ),
        )

# ── Fabric Config Persistence ──────────────────────────────────────────────────
FABRIC_API = "https://api.fabric.microsoft.com/v1"
_CONFIG_DIR = Path(os.environ.get("HOME", "/tmp")) / ".fabric-configs"
_CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def _config_path(workspace_name: str) -> Path:
    safe_name = re.sub(r'[^a-zA-Z0-9_-]', '_', workspace_name)
    return _CONFIG_DIR / f"{safe_name}.json"


def load_fabric_config(workspace_name: str) -> dict | None:
    path = _config_path(workspace_name)
    if path.exists():
        return json.loads(path.read_text())
    return None


def save_fabric_config(workspace_name: str, config: dict) -> None:
    path = _config_path(workspace_name)
    path.write_text(json.dumps(config, indent=2))


def delete_fabric_config(workspace_name: str) -> bool:
    path = _config_path(workspace_name)
    if path.exists():
        path.unlink()
        return True
    return False


def _require_token(token: str | None, token_type: str) -> str:
    """Validate that a user-provided token was supplied."""
    if not token:
        raise HTTPException(
            status_code=401,
            detail=(
                f"Missing {token_type} token. Provide it via the header shown in Swagger UI. "
                f"Get one with: az account get-access-token --resource "
                f"{'https://api.fabric.microsoft.com' if token_type == 'Fabric API' else 'https://database.windows.net'} "
                f"--query accessToken -o tsv"
            ),
        )
    return token


class InsertItem(BaseModel):
    id: str | None = Field(
        default=None,
        description="Optional item id. If omitted, the API will generate a UUID.",
        examples=["5f6b7c8d-1234-4abc-9def-0123456789ab"],
    )
    name: str = Field(
        ...,
        description="Short name/title for the item.",
        examples=["Test Item"],
    )
    description: str = Field(
        ...,
        description="Free-form description for the item.",
        examples=["Inserted via Swagger UI"],
    )


class GenerateDataRequest(BaseModel):
    batch_size: int = Field(default=100, ge=1, le=100000, description="Number of items to generate")
    delay_ms: int = Field(default=100, ge=0, le=5000, description="Delay between inserts in milliseconds")


class FailoverRequest(BaseModel):
    target_region: str = Field(
        ...,
        description="Azure region to make the new primary write region",
        examples=["australiasoutheast"],
    )


class VerifyMirrorRequest(BaseModel):
    batch_id: str = Field(..., description="Batch ID to verify", examples=["a1b2c3d4"])
    workspace_name: str = Field(
        ...,
        description="Fabric workspace name (loads saved config)",
        examples=["MyWorkspace"],
    )


class SaveFabricConfigRequest(BaseModel):
    workspace_name: str = Field(..., description="Fabric workspace display name", examples=["MyWorkspace"])
    workspace_id: str = Field(..., description="Fabric workspace ID (GUID)")
    database_display_name: str = Field(..., description="Mirrored database display name")
    sql_endpoint: str = Field(..., description="SQL Analytics Endpoint connection string")
    table_schema: str = Field(default="dbo", description="Table schema name")
    table_name: str = Field(..., description="Table name in the mirrored database")


class FabricSetupRequest(BaseModel):
    workspace_name: str = Field(..., description="Fabric workspace display name")
    database_name: str = Field(..., description="Mirrored database artifact name")
    table_name: str = Field(..., description="Table name to query")
    table_schema: str = Field(default="dbo", description="Table schema")


@lru_cache(maxsize=1)
def get_cosmos_container():
    if not cosmos_endpoint or not database_name or not container_name:
        missing = [
            name
            for name, value in [
                ("COSMOS_ENDPOINT", cosmos_endpoint),
                ("COSMOS_DATABASE_NAME", database_name),
                ("COSMOS_CONTAINER_NAME", container_name),
            ]
            if not value
        ]
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")

    client = CosmosClient(url=cosmos_endpoint, credential=_credential)
    database = client.get_database_client(database_name)
    return database.get_container_client(container_name)


@lru_cache(maxsize=1)
def get_management_client():
    if not subscription_id:
        raise RuntimeError("AZURE_SUBSCRIPTION_ID not configured")
    return CosmosDBManagementClient(_credential, subscription_id)


def _get_async_container():
    """Get the singleton async Cosmos container client (lazy-initialized once)."""
    global _async_credential, _async_cosmos_client, _async_cosmos_container
    if not cosmos_endpoint or not database_name or not container_name:
        raise RuntimeError("Cosmos DB not configured")
    if _async_cosmos_container is None:
        _async_credential = AsyncDefaultAzureCredential()
        _async_cosmos_client = AsyncCosmosClient(url=cosmos_endpoint, credential=_async_credential)
        database = _async_cosmos_client.get_database_client(database_name)
        _async_cosmos_container = database.get_container_client(container_name)
    return _async_cosmos_container


@app.on_event("shutdown")
async def _shutdown_async_clients():
    global _async_credential, _async_cosmos_client, _async_cosmos_container
    try:
        if _async_cosmos_client is not None:
            await _async_cosmos_client.close()
    except Exception:
        pass
    try:
        if _async_credential is not None:
            await _async_credential.close()
    except Exception:
        pass
    _async_credential = None
    _async_cosmos_client = None
    _async_cosmos_container = None


def get_fabric_connection(sql_endpoint: str, database_display_name: str, sql_token: str):
    """Create a fresh pyodbc connection to a Fabric SQL Analytics Endpoint using a user-provided token."""
    if not _pyodbc_available:
        raise HTTPException(
            status_code=500,
            detail="pyodbc is not available. The ODBC Driver 18 may not be installed on this host. "
            "Add a startup script to install it, or use a custom Docker image.",
        )
    token_bytes = sql_token.encode("UTF-16-LE")
    token_struct = struct.pack(f"<I{len(token_bytes)}s", len(token_bytes), token_bytes)
    conn_str = (
        f"DRIVER={{ODBC Driver 18 for SQL Server}};"
        f"SERVER={sql_endpoint};"
        f"DATABASE={database_display_name};"
        f"Encrypt=yes;TrustServerCertificate=no"
    )
    return pyodbc.connect(conn_str, autocommit=True, attrs_before={1256: token_struct})


def _validate_table_name(name: str) -> str:
    """Validate table name contains only safe characters."""
    if not re.match(r'^[a-zA-Z0-9_-]+$', name):
        raise HTTPException(status_code=500, detail="Invalid table name configuration")
    return name

@app.get("/", response_class=HTMLResponse)
def dashboard():
    """Serve the test harness dashboard."""
    html_path = Path(__file__).parent / "static" / "index.html"
    if html_path.exists():
        return HTMLResponse(content=html_path.read_text())
    return HTMLResponse(content="<h1>Dashboard not found</h1>", status_code=500)


@app.get("/api/health")
def health_check():
    return {
        "status": "running",
        "message": "Cosmos DB Mirror Failover Test API",
    }


@app.get("/api/identity")
def whoami():
    """Return identity info for granting Fabric workspace permissions.

    Decodes the Fabric API access token to surface the appid/oid/upn that the
    user must add to the Fabric workspace as Contributor.
    """
    import base64
    try:
        token = _credential.get_token("https://api.fabric.microsoft.com/.default").token
        # JWT: header.payload.signature
        parts = token.split(".")
        if len(parts) < 2:
            return {"error": "Unexpected token format"}
        payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload_b64))
        return {
            "appId": claims.get("appid") or claims.get("azp"),
            "objectId": claims.get("oid"),
            "tenantId": claims.get("tid"),
            "upnOrName": claims.get("upn") or claims.get("unique_name") or claims.get("name"),
            "appServiceName": os.environ.get("WEBSITE_SITE_NAME"),
            "instructions": (
                "In the Fabric workspace, click 'Manage access' and add the appId or objectId above as Contributor. "
                "Search by the App Service name (appServiceName) — it will appear in the picker."
            ),
        }
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/userAuth/start")
def user_auth_start(tenant_id: str | None = None):
    """Start a device code sign-in flow for the user. Returns the code/URL to display."""
    global _user_signed_in
    with _device_flow_lock:
        if _device_flow_state["in_progress"]:
            return {
                "status": "in_progress",
                "user_code": _device_flow_state["user_code"],
                "verification_uri": _device_flow_state["verification_uri"],
                "message": _device_flow_state["message"],
            }
        # Reset state for a fresh flow
        _device_flow_state["in_progress"] = True
        _device_flow_state["user_code"] = None
        _device_flow_state["verification_uri"] = None
        _device_flow_state["message"] = None
        _device_flow_state["error"] = None
    _user_signed_in = False
    t = threading.Thread(target=_start_device_flow_thread, args=(tenant_id,), daemon=True)
    t.start()
    # Wait briefly for the prompt callback to populate the code
    for _ in range(50):
        with _device_flow_lock:
            if _device_flow_state["user_code"] or _device_flow_state["error"]:
                break
        time.sleep(0.1)
    with _device_flow_lock:
        return {
            "status": "started",
            "user_code": _device_flow_state["user_code"],
            "verification_uri": _device_flow_state["verification_uri"],
            "message": _device_flow_state["message"],
            "error": _device_flow_state["error"],
        }


@app.get("/api/userAuth/status")
def user_auth_status():
    """Poll the status of a user device-code sign-in."""
    pasted_valid = bool(
        _user_pasted_sql_token
        and _user_pasted_sql_token_exp
        and _user_pasted_sql_token_exp > int(time.time()) + 30
    )
    pasted_expires_in = (
        max(0, _user_pasted_sql_token_exp - int(time.time())) if _user_pasted_sql_token_exp else None
    )
    with _device_flow_lock:
        return {
            "signed_in": _user_signed_in,
            "upn": _user_upn,
            "in_progress": _device_flow_state["in_progress"],
            "user_code": _device_flow_state["user_code"],
            "verification_uri": _device_flow_state["verification_uri"],
            "message": _device_flow_state["message"],
            "error": _device_flow_state["error"],
            "pasted_token_valid": pasted_valid,
            "pasted_token_expires_in_sec": pasted_expires_in,
        }


class PastedTokenRequest(BaseModel):
    token: str = Field(..., description="JWT access token for https://database.windows.net")


@app.post("/api/userAuth/pasteToken")
def user_auth_paste_token(request: PastedTokenRequest):
    """Accept a pasted SQL access token (e.g. from `az account get-access-token --resource https://database.windows.net`)."""
    global _user_pasted_sql_token, _user_pasted_sql_token_exp, _user_upn
    token = request.token.strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    parts = token.split(".")
    if len(parts) < 2:
        raise HTTPException(status_code=400, detail="Token does not look like a JWT (missing '.' separators)")
    try:
        import base64
        payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload_b64))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to decode token payload: {exc}")
    aud = claims.get("aud", "")
    if "database.windows.net" not in aud and "database" not in aud:
        raise HTTPException(
            status_code=400,
            detail=f"Token audience is '{aud}'. Expected a token for https://database.windows.net. "
            "Run: az account get-access-token --resource https://database.windows.net --query accessToken -o tsv",
        )
    exp = int(claims.get("exp", 0))
    if exp <= int(time.time()):
        raise HTTPException(status_code=400, detail="Token is already expired")
    _user_pasted_sql_token = token
    _user_pasted_sql_token_exp = exp
    _user_upn = claims.get("upn") or claims.get("preferred_username") or claims.get("unique_name") or _user_upn
    return {
        "ok": True,
        "upn": _user_upn,
        "expires_in_sec": exp - int(time.time()),
    }


@app.post("/api/userAuth/signOut")
def user_auth_sign_out():
    """Clear the user credential so subsequent calls fall back to the managed identity."""
    global _user_credential, _user_signed_in, _user_upn, _user_pasted_sql_token, _user_pasted_sql_token_exp
    _user_credential = None
    _user_signed_in = False
    _user_upn = None
    _user_pasted_sql_token = None
    _user_pasted_sql_token_exp = None
    with _device_flow_lock:
        _device_flow_state["in_progress"] = False
        _device_flow_state["user_code"] = None
        _device_flow_state["verification_uri"] = None
        _device_flow_state["message"] = None
        _device_flow_state["error"] = None
    return {"signed_in": False}


@app.post("/api/insertData", status_code=201)
def insert_data(payload: InsertItem):
    try:
        container = get_cosmos_container()

        item_id = payload.id or str(uuid.uuid4())
        item = {
            "id": item_id,
            "name": payload.name,
            "description": payload.description,
            "timestamp": datetime.utcnow().isoformat(),
        }

        created_item = container.create_item(body=item)
        return {
            "message": "Data inserted successfully",
            "id": created_item.get("id"),
            "item": created_item,
        }
    except Exception as error:
        status_code = getattr(error, "status_code", None)
        if status_code == 429:
            raise HTTPException(
                status_code=429,
                detail="Request rate too large. Please retry after some time.",
            )
        if status_code == 409:
            raise HTTPException(status_code=409, detail="Item with this ID already exists")

        raise HTTPException(status_code=500, detail=str(error))

@app.get("/api/queryData")
def query_data():
    try:
        container = get_cosmos_container()

        query = "SELECT VALUE c FROM c ORDER BY c.sequenceNumber"
        items = list(
            container.query_items(
                query=query,
                enable_cross_partition_query=True,
            )
        )
        return {"count": len(items), "items": items}
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error))


@app.get("/api/countData")
def count_data():
    try:
        container = get_cosmos_container()
        result = list(
            container.query_items(
                query="SELECT VALUE COUNT(1) FROM c",
                enable_cross_partition_query=True,
            )
        )
        return {"count": result[0] if result else 0}
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error))


@app.delete("/api/resetData")
def reset_data():
    """Delete all items in the container."""
    try:
        container = get_cosmos_container()
        items = list(
            container.query_items(
                query="SELECT c.id FROM c",
                enable_cross_partition_query=True,
            )
        )
        deleted = 0
        errors = []
        for item in items:
            try:
                container.delete_item(item=item["id"], partition_key=item["id"])
                deleted += 1
            except Exception as e:
                errors.append({"id": item["id"], "error": str(e)})
        return {"deleted": deleted, "errors": len(errors), "errorDetails": errors[:10]}
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error))


# ── Failover Test Endpoints ────────────────────────────────────────────────────


async def _lookup_write_region() -> str:
    """Get the current Cosmos write region without blocking the event loop."""
    def _do():
        try:
            mgmt = get_management_client()
            acct = mgmt.database_accounts.get(resource_group, cosmos_account_name)
            return acct.write_locations[0].location_name if acct.write_locations else "unknown"
        except Exception:
            return "unknown"
    return await asyncio.to_thread(_do)


async def _run_generate_batch(batch_id: str, request: GenerateDataRequest):
    """Background task that generates items and updates _batch_progress."""
    progress = _batch_progress[batch_id]
    container = _get_async_container()
    # Resolve write region in the background (don't block the POST response)
    if progress.get("writeRegion") in (None, "unknown", "resolving"):
        progress["writeRegion"] = await _lookup_write_region()
    try:
        for seq in range(1, request.batch_size + 1):
            if _batch_cancel.get(batch_id):
                progress["cancelled"] = True
                break
            # Refresh write region every 5 items so we can SEE failover happen mid-batch
            if seq % 5 == 1 and seq > 1:
                progress["writeRegion"] = await _lookup_write_region()
            item = {
                "id": f"{batch_id}-{seq:06d}",
                "batchId": batch_id,
                "sequenceNumber": seq,
                "writeRegion": progress["writeRegion"],
                "payload": f"Failover test data - batch {batch_id}, sequence {seq}",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            try:
                # Per-insert timeout so a failover-induced hang counts as a failure
                # rather than stalling the whole batch indefinitely.
                await asyncio.wait_for(container.create_item(body=item), timeout=30)
                progress["succeeded"] += 1
            except asyncio.TimeoutError:
                progress["failed"] += 1
                progress["timeouts"] = progress.get("timeouts", 0) + 1
                if len(progress["errors"]) < 20:
                    progress["errors"].append({"sequenceNumber": seq, "error": "timeout (30s) — likely failover in progress"})
            except Exception as e:
                progress["failed"] += 1
                if len(progress["errors"]) < 20:
                    progress["errors"].append({"sequenceNumber": seq, "error": str(e)})
            progress["seq"] = seq
            if request.delay_ms > 0 and seq < request.batch_size:
                await asyncio.sleep(request.delay_ms / 1000.0)
    except Exception as e:
        progress["fatalError"] = str(e)
    finally:
        progress["status"] = "done"
        progress["finishedAt"] = datetime.now(timezone.utc).isoformat()
        _batch_cancel.pop(batch_id, None)


@app.post("/api/generateData")
async def generate_data(request: GenerateDataRequest):
    """Start an async batch insert. Returns batchId immediately; poll /api/batchProgress/{batchId}."""
    batch_id = str(uuid.uuid4())[:8]

    _batch_progress[batch_id] = {
        "batchId": batch_id,
        "requested": request.batch_size,
        "writeRegion": "resolving",
        "seq": 0,
        "succeeded": 0,
        "failed": 0,
        "errors": [],
        "status": "running",
        "startedAt": datetime.now(timezone.utc).isoformat(),
    }

    # Fire and forget — runs on the event loop. Keep a strong reference.
    task = asyncio.create_task(_run_generate_batch(batch_id, request))
    _running_tasks.add(task)
    task.add_done_callback(_running_tasks.discard)

    return {
        "batchId": batch_id,
        "requested": request.batch_size,
        "writeRegion": "resolving",
        "status": "running",
    }


@app.get("/api/batchProgress/{batch_id}")
def get_batch_progress(batch_id: str):
    """Poll the progress of an in-flight or finished batch."""
    progress = _batch_progress.get(batch_id)
    if not progress:
        raise HTTPException(status_code=404, detail=f"Batch {batch_id} not found")
    return progress


@app.post("/api/stopBatch/{batch_id}")
def stop_batch(batch_id: str):
    """Request graceful cancellation of a running batch."""
    if batch_id not in _batch_progress:
        raise HTTPException(status_code=404, detail=f"Batch {batch_id} not found")
    _batch_cancel[batch_id] = True
    return {"batchId": batch_id, "cancelRequested": True}


@app.get("/api/accountStatus")
def get_account_status():
    """Get current Cosmos DB account status including region configuration."""
    try:
        if not resource_group or not cosmos_account_name:
            raise HTTPException(
                status_code=500,
                detail="Management API not configured (AZURE_RESOURCE_GROUP, COSMOS_ACCOUNT_NAME required)",
            )

        mgmt_client = get_management_client()
        account = mgmt_client.database_accounts.get(resource_group, cosmos_account_name)

        return {
            "name": account.name,
            "provisioningState": account.provisioning_state,
            "locations": [
                {
                    "locationName": loc.location_name,
                    "failoverPriority": loc.failover_priority,
                    "isZoneRedundant": loc.is_zone_redundant,
                }
                for loc in sorted(account.locations, key=lambda l: l.failover_priority)
            ],
            "writeLocations": [loc.location_name for loc in account.write_locations],
            "readLocations": [loc.location_name for loc in account.read_locations],
        }
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error))


@app.post("/api/failover")
def trigger_failover(request: FailoverRequest):
    """Trigger a manual regional failover on the Cosmos DB account."""
    try:
        if not resource_group or not cosmos_account_name:
            raise HTTPException(status_code=500, detail="Management API not configured")

        mgmt_client = get_management_client()
        account = mgmt_client.database_accounts.get(resource_group, cosmos_account_name)

        # Normalize region names for comparison
        def normalize(name: str) -> str:
            return name.lower().replace(" ", "")

        target = normalize(request.target_region)
        account_regions = {normalize(loc.location_name): loc.location_name for loc in account.locations}

        if target not in account_regions:
            raise HTTPException(
                status_code=400,
                detail=f"Region '{request.target_region}' not found on account. Available: {list(account_regions.values())}",
            )

        # Check if already primary
        current_primary = next((loc for loc in account.write_locations), None)
        if current_primary and normalize(current_primary.location_name) == target:
            return {
                "message": f"'{request.target_region}' is already the primary write region",
                "status": "NoChange",
            }

        # Build failover policies: target becomes priority 0, others follow
        failover_policies = [{"location_name": account_regions[target], "failover_priority": 0}]
        priority = 1
        for norm_name, display_name in account_regions.items():
            if norm_name != target:
                failover_policies.append({"location_name": display_name, "failover_priority": priority})
                priority += 1

        # Trigger failover (async ARM operation — returns immediately)
        mgmt_client.database_accounts.begin_failover_priority_change(
            resource_group,
            cosmos_account_name,
            failover_parameters={"failover_policies": failover_policies},
        )

        return {
            "message": "Failover initiated",
            "targetRegion": request.target_region,
            "newFailoverPolicies": failover_policies,
            "status": "InProgress",
            "note": "Use GET /api/accountStatus to monitor progress",
        }
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error))


@app.post("/api/verifyMirror")
def verify_mirror(
    request: VerifyMirrorRequest,
    x_sql_token: str | None = Header(
        default=None,
        description="Optional SQL token override. If omitted, the app's managed identity is used.",
    ),
):
    """Compare batch data between Cosmos DB and the Fabric mirrored database.
    Uses saved Fabric config for the given workspace_name. Requires an x-sql-token header."""
    try:
        # Query Cosmos DB for the batch
        container = get_cosmos_container()
        cosmos_query = (
            "SELECT c.id, c.sequenceNumber FROM c "
            "WHERE c.batchId = @batchId ORDER BY c.sequenceNumber"
        )
        cosmos_items = list(
            container.query_items(
                query=cosmos_query,
                parameters=[{"name": "@batchId", "value": request.batch_id}],
                enable_cross_partition_query=True,
            )
        )
        cosmos_sequences = sorted({item["sequenceNumber"] for item in cosmos_items})

        # Load Fabric config for the workspace
        config = load_fabric_config(request.workspace_name)
        if not config:
            return {
                "batchId": request.batch_id,
                "cosmos": {"count": len(cosmos_items), "sequences": cosmos_sequences},
                "fabric": {
                    "error": (
                        f"No Fabric config saved for workspace '{request.workspace_name}'. "
                        "Use the Fabric setup endpoints to discover and save a configuration first."
                    )
                },
                "comparison": None,
            }

        table_schema = config.get("table_schema", "dbo")
        table = _validate_table_name(config["table_name"])
        schema = _validate_table_name(table_schema)

        sql_token = _resolve_token(x_sql_token, "https://database.windows.net/.default")
        conn = get_fabric_connection(config["sql_endpoint"], config["database_display_name"], sql_token)
        try:
            cursor = conn.cursor()
            cursor.execute(
                f"SELECT id, sequenceNumber FROM [{schema}].[{table}] WHERE batchId = ? ORDER BY sequenceNumber",
                request.batch_id,
            )
            fabric_rows = cursor.fetchall()
        finally:
            conn.close()

        fabric_sequences = sorted({row.sequenceNumber for row in fabric_rows})

        cosmos_set = set(cosmos_sequences)
        fabric_set = set(fabric_sequences)
        missing_in_fabric = sorted(cosmos_set - fabric_set)
        extra_in_fabric = sorted(fabric_set - cosmos_set)

        return {
            "batchId": request.batch_id,
            "workspaceName": request.workspace_name,
            "cosmos": {"count": len(cosmos_items), "maxSequence": max(cosmos_sequences) if cosmos_sequences else 0},
            "fabric": {"count": len(fabric_rows), "maxSequence": max(fabric_sequences) if fabric_sequences else 0},
            "comparison": {
                "match": cosmos_set == fabric_set,
                "missingInFabric": missing_in_fabric,
                "missingInFabricCount": len(missing_in_fabric),
                "extraInFabric": extra_in_fabric,
                "extraInFabricCount": len(extra_in_fabric),
            },
        }
    except HTTPException:
        raise
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error))


# ── Fabric Discovery & Config Endpoints ────────────────────────────────────────


@app.get("/api/fabricWorkspaces")
def list_fabric_workspaces(
    x_fabric_token: str | None = Header(default=None, description="Optional override; MI used if omitted"),
):
    """List Fabric workspaces accessible to the caller's identity."""
    try:
        token = _resolve_token(x_fabric_token, "https://api.fabric.microsoft.com/.default")
        headers = {"Authorization": f"Bearer {token}"}
        workspaces = []
        url = f"{FABRIC_API}/workspaces"

        while url:
            resp = http_requests.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            workspaces.extend(data.get("value", []))
            url = data.get("continuationUri")

        return {
            "count": len(workspaces),
            "workspaces": [
                {"id": ws["id"], "displayName": ws["displayName"], "type": ws.get("type")}
                for ws in workspaces
            ],
        }
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error))


@app.get("/api/fabricDatabases/{workspace_id}")
def list_fabric_databases(
    workspace_id: str,
    x_fabric_token: str | None = Header(default=None, description="Optional override; MI used if omitted"),
):
    """List mirrored databases in a Fabric workspace (includes SQL endpoint info)."""
    try:
        token = _resolve_token(x_fabric_token, "https://api.fabric.microsoft.com/.default")
        headers = {"Authorization": f"Bearer {token}"}
        databases = []
        url = f"{FABRIC_API}/workspaces/{workspace_id}/mirroredDatabases"

        while url:
            resp = http_requests.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            databases.extend(data.get("value", []))
            url = data.get("continuationUri")

        return {
            "workspaceId": workspace_id,
            "count": len(databases),
            "databases": [
                {
                    "id": db["id"],
                    "displayName": db["displayName"],
                    "sqlEndpoint": db.get("properties", {})
                    .get("sqlEndpointProperties", {})
                    .get("connectionString"),
                    "provisioningStatus": db.get("properties", {})
                    .get("sqlEndpointProperties", {})
                    .get("provisioningStatus"),
                }
                for db in databases
            ],
        }
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error))


@app.get("/api/fabricTables/{workspace_id}/{database_display_name}")
def list_fabric_tables(
    workspace_id: str,
    database_display_name: str,
    x_fabric_token: str | None = Header(default=None, description="Optional override; MI used if omitted"),
    x_sql_token: str | None = Header(default=None, description="Optional override; MI used if omitted"),
):
    """List tables in a mirrored database by connecting to its SQL Analytics Endpoint."""
    try:
        # Look up the SQL endpoint from the mirrored database API
        token = _resolve_token(x_fabric_token, "https://api.fabric.microsoft.com/.default")
        headers = {"Authorization": f"Bearer {token}"}
        url = f"{FABRIC_API}/workspaces/{workspace_id}/mirroredDatabases"
        sql_endpoint = None

        while url:
            resp = http_requests.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            for db in data.get("value", []):
                if db["displayName"] == database_display_name:
                    sql_endpoint = (
                        db.get("properties", {})
                        .get("sqlEndpointProperties", {})
                        .get("connectionString")
                    )
                    break
            if sql_endpoint:
                break
            url = data.get("continuationUri")

        if not sql_endpoint:
            raise HTTPException(
                status_code=404,
                detail=f"Mirrored database '{database_display_name}' not found in workspace",
            )

        sql_tok = _resolve_token(x_sql_token, "https://database.windows.net/.default")
        conn = get_fabric_connection(sql_endpoint, database_display_name, sql_tok)
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE "
                "FROM INFORMATION_SCHEMA.TABLES "
                "ORDER BY TABLE_SCHEMA, TABLE_NAME"
            )
            tables = [
                {"schema": row.TABLE_SCHEMA, "table": row.TABLE_NAME, "type": row.TABLE_TYPE}
                for row in cursor.fetchall()
            ]
        finally:
            conn.close()

        return {
            "workspaceId": workspace_id,
            "databaseDisplayName": database_display_name,
            "sqlEndpoint": sql_endpoint,
            "count": len(tables),
            "tables": tables,
        }
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error))


@app.get("/api/fabricConfig/{workspace_name}")
def get_fabric_config(workspace_name: str):
    """Get saved Fabric configuration for a workspace."""
    config = load_fabric_config(workspace_name)
    if not config:
        raise HTTPException(status_code=404, detail=f"No configuration saved for workspace '{workspace_name}'")
    return {"workspaceName": workspace_name, "config": config}


@app.post("/api/fabricConfig/{workspace_name}", status_code=201)
def save_fabric_config_endpoint(workspace_name: str, request: SaveFabricConfigRequest):
    """Save Fabric configuration for a workspace. Persisted locally so it survives restarts."""
    config = {
        "workspace_name": request.workspace_name,
        "workspace_id": request.workspace_id,
        "database_display_name": request.database_display_name,
        "sql_endpoint": request.sql_endpoint,
        "table_schema": request.table_schema,
        "table_name": request.table_name,
    }
    save_fabric_config(workspace_name, config)
    return {"message": f"Configuration saved for workspace '{workspace_name}'", "config": config}


@app.delete("/api/fabricConfig/{workspace_name}")
def delete_fabric_config_endpoint(workspace_name: str):
    """Delete saved Fabric configuration for a workspace."""
    if delete_fabric_config(workspace_name):
        return {"message": f"Configuration deleted for workspace '{workspace_name}'"}
    raise HTTPException(status_code=404, detail=f"No configuration found for workspace '{workspace_name}'")


@app.post("/api/fabricSetup")
def fabric_setup(
    request: FabricSetupRequest,
    x_fabric_token: str | None = Header(default=None, description="Optional override; MI used if omitted"),
):
    """One-step Fabric setup: provide workspace name, mirrored DB name, and table name.
    Resolves the SQL endpoint via Fabric API and saves config."""
    try:
        token = _resolve_token(x_fabric_token, "https://api.fabric.microsoft.com/.default")
        headers = {"Authorization": f"Bearer {token}"}

        # Find workspace by name
        workspace_id = None
        url = f"{FABRIC_API}/workspaces"
        while url:
            resp = http_requests.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            for ws in data.get("value", []):
                if ws["displayName"].lower() == request.workspace_name.lower():
                    workspace_id = ws["id"]
                    break
            if workspace_id:
                break
            url = data.get("continuationUri")

        if not workspace_id:
            raise HTTPException(status_code=404, detail=f"Workspace '{request.workspace_name}' not found")

        # Find mirrored database and its SQL endpoint
        sql_endpoint = None
        url = f"{FABRIC_API}/workspaces/{workspace_id}/mirroredDatabases"
        while url:
            resp = http_requests.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            for db in data.get("value", []):
                if db["displayName"].lower() == request.database_name.lower():
                    sql_endpoint = (
                        db.get("properties", {})
                        .get("sqlEndpointProperties", {})
                        .get("connectionString")
                    )
                    break
            if sql_endpoint:
                break
            url = data.get("continuationUri")

        if not sql_endpoint:
            raise HTTPException(
                status_code=404,
                detail=f"Mirrored database '{request.database_name}' not found in workspace '{request.workspace_name}'",
            )

        # Save config
        config = {
            "workspace_name": request.workspace_name,
            "workspace_id": workspace_id,
            "database_display_name": request.database_name,
            "sql_endpoint": sql_endpoint,
            "table_schema": request.table_schema,
            "table_name": request.table_name,
        }
        save_fabric_config(request.workspace_name, config)
        return {
            "message": "Fabric configuration saved",
            "config": config,
        }
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error))

@app.get("/api/fabricDiagnose/{workspace_name}")
def fabric_diagnose(
    workspace_name: str,
    x_sql_token: str | None = Header(default=None),
):
    """Run multiple connection attempts to isolate token vs DB-name vs permission issues."""
    config = load_fabric_config(workspace_name)
    if not config:
        raise HTTPException(status_code=404, detail=f"No config for '{workspace_name}'")
    sql_token = _resolve_token(x_sql_token, "https://database.windows.net/.default")
    # Decode token to confirm identity
    identity = {"upn": None, "oid": None, "aud": None, "exp": None, "expires_in_sec": None}
    try:
        import base64
        parts = sql_token.split(".")
        payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload_b64))
        identity["upn"] = claims.get("upn") or claims.get("preferred_username") or claims.get("unique_name")
        identity["oid"] = claims.get("oid")
        identity["aud"] = claims.get("aud")
        identity["exp"] = claims.get("exp")
        if claims.get("exp"):
            identity["expires_in_sec"] = int(claims["exp"]) - int(time.time())
    except Exception as exc:
        identity["decode_error"] = str(exc)

    sql_endpoint = config["sql_endpoint"]
    db_name = config["database_display_name"]
    table_schema = config.get("table_schema", "dbo")
    table_name = config["table_name"]

    def try_connect(database: str | None):
        result = {"database": database or "(none)", "ok": False, "error": None, "rows": None}
        try:
            token_bytes = sql_token.encode("UTF-16-LE")
            token_struct = struct.pack(f"<I{len(token_bytes)}s", len(token_bytes), token_bytes)
            conn_str = f"DRIVER={{ODBC Driver 18 for SQL Server}};SERVER={sql_endpoint};Encrypt=yes;TrustServerCertificate=no"
            if database:
                conn_str += f";DATABASE={database}"
            conn = pyodbc.connect(conn_str, autocommit=True, attrs_before={1256: token_struct}, timeout=15)
            try:
                cur = conn.cursor()
                cur.execute("SELECT DB_NAME() AS db, SUSER_SNAME() AS me, @@VERSION AS v")
                row = cur.fetchone()
                result["rows"] = {"db": row.db, "me": row.me, "version": (row.v or "")[:80]}
                result["ok"] = True
            finally:
                conn.close()
        except Exception as exc:
            result["error"] = str(exc)
        return result

    attempts = []
    # 1. No DATABASE specified
    attempts.append({"label": "Connect without DATABASE", **try_connect(None)})
    # 2. With configured DB name
    attempts.append({"label": f"Connect with DATABASE='{db_name}'", **try_connect(db_name)})
    # 3. List databases visible to this user (only runs if any of the above worked)
    visible_dbs = None
    visible_dbs_error = None
    if any(a["ok"] for a in attempts):
        try:
            token_bytes = sql_token.encode("UTF-16-LE")
            token_struct = struct.pack(f"<I{len(token_bytes)}s", len(token_bytes), token_bytes)
            conn_str = f"DRIVER={{ODBC Driver 18 for SQL Server}};SERVER={sql_endpoint};Encrypt=yes;TrustServerCertificate=no"
            conn = pyodbc.connect(conn_str, autocommit=True, attrs_before={1256: token_struct}, timeout=15)
            try:
                cur = conn.cursor()
                cur.execute("SELECT name FROM sys.databases ORDER BY name")
                visible_dbs = [r.name for r in cur.fetchall()]
            finally:
                conn.close()
        except Exception as exc:
            visible_dbs_error = str(exc)

    return {
        "tokenSource": _last_token_source,
        "identity": identity,
        "sqlEndpoint": sql_endpoint,
        "configuredDatabase": db_name,
        "configuredSchema": table_schema,
        "configuredTable": table_name,
        "attempts": attempts,
        "visibleDatabases": visible_dbs,
        "visibleDatabasesError": visible_dbs_error,
    }


@app.get("/api/fabricColumns/{workspace_name}")
def get_fabric_columns(
    workspace_name: str,
    x_sql_token: str | None = Header(default=None, description="Optional override; MI used if omitted"),
):
    """List columns in the configured mirrored table."""
    config = load_fabric_config(workspace_name)
    if not config:
        raise HTTPException(status_code=404, detail=f"No config for '{workspace_name}'")
    sql_token = _resolve_token(x_sql_token, "https://database.windows.net/.default")
    table_schema = _validate_table_name(config.get("table_schema", "dbo"))
    table_name = _validate_table_name(config["table_name"])
    try:
        conn = get_fabric_connection(config["sql_endpoint"], config["database_display_name"], sql_token)
    except Exception as exc:
        msg = str(exc)
        if "18456" in msg:
            src = _last_token_source
            had_pasted = _user_pasted_sql_token is not None
            pasted_expired = (
                had_pasted
                and _user_pasted_sql_token_exp is not None
                and _user_pasted_sql_token_exp <= int(time.time()) + 30
            )
            if src == "managed_identity" and pasted_expired:
                hint = (
                    "Your pasted token EXPIRED, so the harness fell back to the managed identity "
                    "(which has no Fabric access). Click 'Sign out' then 'Paste a token instead' "
                    "and paste a fresh token from: az account get-access-token --resource https://database.windows.net --query accessToken -o tsv"
                )
            elif src == "managed_identity":
                hint = (
                    "Using the managed identity token, which has no access to this mirrored database. "
                    "Either click 'Paste a token instead' and use your own SQL token, OR in Fabric portal "
                    "open the workspace > Manage access and add this App Service's managed identity "
                    "as Contributor (look up the principal ID with `az webapp identity show -g <rg> -n <app>`)."
                )
            elif src == "pasted":
                hint = (
                    "Your pasted token was accepted by Fabric but the user has no access to this mirrored "
                    "database. Make sure your account has at least Viewer on the Fabric workspace."
                )
            elif src == "device_code":
                hint = (
                    "Your signed-in user has no access to this mirrored database. Make sure your account "
                    "has at least Viewer on the Fabric workspace."
                )
            else:
                hint = "The provided token was rejected."
            raise HTTPException(
                status_code=403,
                detail=f"Fabric SQL login failed (18456). {hint} Token source: {src}. Raw: {msg}",
            )
        raise HTTPException(status_code=500, detail=f"SQL connection failed: {msg}")
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? ORDER BY ORDINAL_POSITION",
            table_schema, table_name,
        )
        columns = [{"name": row.COLUMN_NAME, "type": row.DATA_TYPE} for row in cursor.fetchall()]
    finally:
        conn.close()
    return {"schema": table_schema, "table": table_name, "columns": columns}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
