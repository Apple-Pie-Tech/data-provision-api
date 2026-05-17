from fastapi.testclient import TestClient

from app.main import app, create_app, get_point_reader
from app.config import Settings
from app.universe import assemble_universe_graph  # pyright: ignore[reportMissingImports]
from app.vector_store import VectorPoint  # pyright: ignore[reportMissingImports]


class FakePointReader:
    def __init__(self, points: list[VectorPoint]) -> None:
        self.points = points
        self.calls = 0

    async def read_points(self) -> list[VectorPoint]:
        self.calls += 1
        return self.points


class FailingPointReader:
    def __init__(self) -> None:
        self.calls = 0

    async def read_points(self) -> list[VectorPoint]:
        self.calls += 1
        raise ConnectionError("qdrant unavailable")


def _override_point_reader(points: list[VectorPoint]) -> FakePointReader:
    reader = FakePointReader(points)
    app.dependency_overrides[get_point_reader] = lambda: reader
    return reader


def _clear_overrides() -> None:
    app.dependency_overrides.clear()


def test_health_endpoint() -> None:
    client = TestClient(app)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_universe_preflight_returns_cors_headers_for_allowed_origin() -> None:
    cors_app = create_app(Settings(cors_allow_origins="http://127.0.0.1:8081"))
    cors_client = TestClient(cors_app)

    response = cors_client.options(
        "/universe",
        headers={
            "Origin": "http://127.0.0.1:8081",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:8081"
    assert "GET" in response.headers["access-control-allow-methods"]


def test_universe_endpoint_returns_revised_response_for_populated_universe() -> None:
    reader = _override_point_reader(
        [
            VectorPoint(id="beta-member", label="Beta", is_central=False),
            VectorPoint(id="alpha-central", label="Alpha", is_central=True),
            VectorPoint(id="beta-central", label="Beta", is_central=True),
            VectorPoint(id="alpha-member", label="Alpha", is_central=False),
        ]
    )
    client = TestClient(app)

    try:
        response = client.get("/universe")

        assert response.status_code == 200
        assert response.json() == assemble_universe_graph(reader.points).model_dump(mode="json")
        assert reader.calls == 1
    finally:
        _clear_overrides()


def test_universe_endpoint_returns_empty_response_for_empty_universe() -> None:
    reader = _override_point_reader([])
    client = TestClient(app)

    try:
        response = client.get("/universe")

        assert response.status_code == 200
        assert response.json() == {"points": [], "edges": []}
        assert reader.calls == 1
    finally:
        _clear_overrides()


def test_universe_endpoint_returns_503_when_vector_store_is_unavailable() -> None:
    reader = FailingPointReader()
    app.dependency_overrides[get_point_reader] = lambda: reader
    client = TestClient(app)

    try:
        response = client.get("/universe")

        assert response.status_code == 503
        assert response.json() == {"detail": "vector store unavailable"}
        assert reader.calls == 1
    finally:
        _clear_overrides()


def test_openapi_schema_includes_required_top_level_metadata() -> None:
    schema = app.openapi()

    assert schema["info"]["title"] == "Data Provision API"
    assert schema["info"]["version"] == "0.1.0"
    assert "/health" in schema["paths"]
    assert "/universe" in schema["paths"]
    assert "/podcasts" in schema["paths"]
