from collections.abc import AsyncIterator

from fastapi import Depends, FastAPI, HTTPException
from fastapi.openapi.utils import get_openapi
from qdrant_client import AsyncQdrantClient

from app.config import Settings, get_settings
from app.schemas import UniverseResponse  # pyright: ignore[reportMissingImports]
from app.universe import assemble_universe_graph  # pyright: ignore[reportMissingImports]
from app.vector_store import QdrantPointReader  # pyright: ignore[reportMissingImports]

app = FastAPI(title="Data Provision API", version="0.1.0")


def _strip_title_keys(value: object) -> None:
    if isinstance(value, dict):
        value.pop("title", None)
        for nested_value in value.values():
            _strip_title_keys(nested_value)
    elif isinstance(value, list):
        for item in value:
            _strip_title_keys(item)


def custom_openapi() -> dict[str, object]:
    if app.openapi_schema is not None:
        return app.openapi_schema

    schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
    _strip_title_keys(schema)
    app.openapi_schema = schema
    return schema


app.openapi = custom_openapi


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


async def get_point_reader(
    settings: Settings = Depends(get_settings),
) -> AsyncIterator[QdrantPointReader]:
    client = AsyncQdrantClient(
        url=settings.qdrant_url,
        api_key=settings.qdrant_api_key,
    )
    try:
        yield QdrantPointReader(client, settings.qdrant_collection)
    finally:
        await client.close()


@app.get("/universe", response_model=UniverseResponse)
async def get_universe(
    point_reader: QdrantPointReader = Depends(get_point_reader),
) -> UniverseResponse:
    try:
        points = await point_reader.read_points()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="vector store unavailable") from exc
    return assemble_universe_graph(points)


__all__ = ["app", "Settings", "get_point_reader", "get_settings"]
