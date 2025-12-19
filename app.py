import os
import uuid
from datetime import datetime
from functools import lru_cache

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from azure.cosmos import CosmosClient
from azure.identity import DefaultAzureCredential

app = FastAPI(
    title="Cosmos DB Mirror Test Web API",
    description="Simple API to insert/query items in Cosmos DB configured with private endpoints or virtual network rules.",
    version="1.0.0",
)

# Cosmos DB configuration from environment variables
cosmos_endpoint = os.environ.get('COSMOS_ENDPOINT')
database_name = os.environ.get('COSMOS_DATABASE_NAME')
container_name = os.environ.get('COSMOS_CONTAINER_NAME')

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

    credential = DefaultAzureCredential()
    client = CosmosClient(url=cosmos_endpoint, credential=credential)
    database = client.get_database_client(database_name)
    return database.get_container_client(container_name)

@app.get("/")
def health_check():
    return {
        "status": "running",
        "message": "Cosmos DB Web API",
        "endpoints": {
            "health": "GET /",
            "insert": "POST /api/insertData",
            "query": "GET /api/queryData",
            "docs": "GET /docs",
        },
    }

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

        query = "SELECT c.id, c.name, c.description FROM c ORDER BY c.timestamp DESC OFFSET 0 LIMIT 10"
        items = list(
            container.query_items(
                query=query,
                enable_cross_partition_query=True,
            )
        )
        return {"count": len(items), "items": items}
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error))

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
