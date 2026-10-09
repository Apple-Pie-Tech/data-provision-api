import logging

from fastapi.testclient import TestClient

from app.main import (
    app,
    build_podcast_generation_dependencies,
    create_app,
    get_point_reader,
)
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


class FakeTTSResponse:
    def __init__(self, content: bytes = b"wav-bytes") -> None:
        self.content = content

    def raise_for_status(self) -> None:
        return None


class FakeTTSHTTPClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def post(self, url: str, **kwargs: object) -> FakeTTSResponse:
        self.calls.append({"url": url, **kwargs})
        return FakeTTSResponse()


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


def test_universe_endpoint_logs_the_cause_of_a_503(caplog) -> None:
    """The 503 body says nothing, so the log is the only record of why.

    Pinned because the opaque body is deliberate: an empty or failing /universe
    was bug E7, and a status code with no traceback is what made it hard.
    """
    reader = FailingPointReader()
    app.dependency_overrides[get_point_reader] = lambda: reader
    client = TestClient(app, raise_server_exceptions=False)

    try:
        with caplog.at_level(logging.ERROR, logger="app.main"):
            response = client.get("/universe")

        assert response.status_code == 503
        records = [r for r in caplog.records if r.name == "app.main"]
        assert records, "the cause was swallowed"
        assert records[0].exc_info is not None
        assert "qdrant unavailable" in caplog.text
    finally:
        _clear_overrides()


def test_openapi_schema_includes_required_top_level_metadata() -> None:
    schema = app.openapi()

    assert schema["info"]["title"] == "Data Provision API"
    assert schema["info"]["version"] == "0.1.0"
    assert "/health" in schema["paths"]
    assert "/universe" in schema["paths"]
    assert "/podcasts" in schema["paths"]


def _stub_boto3(monkeypatch) -> dict[str, list[dict[str, object]]]:
    """Capture boto3 client construction instead of resolving real credentials.

    build_podcast_generation_dependencies now builds bedrock-runtime and polly
    clients eagerly, and boto3 resolves credentials at construction. Without this
    these tests would need live AWS credentials to assert pure wiring.
    """
    created: dict[str, list[dict[str, object]]] = {}

    def fake_client(service: str, **kwargs: object) -> object:
        created.setdefault(service, []).append(kwargs)
        return object()

    monkeypatch.setattr("boto3.client", fake_client)
    return created


def test_build_podcast_generation_dependencies_uses_configured_polly_voices(
    monkeypatch,
) -> None:
    created = _stub_boto3(monkeypatch)
    settings = Settings(
        _env_file=None,
        aws_region="eu-west-1",
        polly_engine="neural",
        polly_sample_rate="8000",
        polly_voice_host_a="local-voice-a",
        polly_voice_host_b="local-voice-b",
    )

    dependencies = build_podcast_generation_dependencies(
        settings=settings,
        point_reader=FakePointReader([]),
    )

    tts = dependencies.tts_client
    assert tts.voice_models == {"host_a": "local-voice-a", "host_b": "local-voice-b"}
    assert tts.engine == "neural"
    assert tts.sample_rate == "8000"
    assert created["polly"][0]["region_name"] == "eu-west-1"


def test_build_podcast_generation_dependencies_threads_bedrock_settings(monkeypatch) -> None:
    """E14/E35: the model and region come from Settings, not the environment."""
    created = _stub_boto3(monkeypatch)
    settings = Settings(
        _env_file=None,
        aws_region="eu-west-1",
        bedrock_script_model="amazon.nova-lite-v1:0",
        bedrock_script_max_tokens=777,
    )

    dependencies = build_podcast_generation_dependencies(
        settings=settings,
        point_reader=FakePointReader([]),
    )

    generator = dependencies.script_generator
    assert generator.model == "amazon.nova-lite-v1:0"
    assert generator.max_tokens == 777
    assert generator.max_parts == settings.podcast_max_script_parts
    assert created["bedrock-runtime"][0]["region_name"] == "eu-west-1"


def test_build_podcast_generation_dependencies_threads_fal_key(monkeypatch) -> None:
    """fal.ai is the one AI dependency deliberately left outside AWS."""
    _stub_boto3(monkeypatch)
    monkeypatch.delenv("FAL_KEY", raising=False)
    settings = Settings(
        _env_file=None,
        fal_key="fal-key-from-settings",
    )

    dependencies = build_podcast_generation_dependencies(
        settings=settings,
        point_reader=FakePointReader([]),
    )

    assert dependencies.cover_generator is not None
    assert dependencies.cover_generator.client.key == "fal-key-from-settings"
