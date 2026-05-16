from __future__ import annotations

from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient

from app.main import (
    Settings,
    app,
    get_point_reader,
    get_podcast_generation_dependencies,
    get_podcast_repository,
    get_settings,
)
from app.podcast_generation import (  # pyright: ignore[reportMissingImports]
    AudioMerger,
    FALLBACK_COVER_PNG,
)
from app.podcast_schemas import PodcastDetail, PodcastScript, PodcastScriptLine
from app.universe import assemble_universe_graph  # pyright: ignore[reportMissingImports]
from app.vector_store import VectorPoint  # pyright: ignore[reportMissingImports]


class FakePointReader:
    def __init__(self, points_by_label: dict[str, list[VectorPoint]]) -> None:
        self.points_by_label = points_by_label
        self.calls: list[str] = []

    async def read_points(self) -> list[VectorPoint]:
        self.calls.append("__all__")
        points: list[VectorPoint] = []
        for label_points in self.points_by_label.values():
            points.extend(label_points)
        return points

    async def read_points_for_label(self, label: str | None) -> list[VectorPoint]:
        normalized_label = "" if label is None else label
        self.calls.append(normalized_label)
        return list(self.points_by_label.get(normalized_label, []))


class FakeScriptGenerator:
    def __init__(self, script: PodcastScript) -> None:
        self.script = script
        self.calls: list[dict[str, object]] = []

    def generate_script(self, *, label: str, chunks: list[str]) -> PodcastScript:
        self.calls.append({"label": label, "chunks": chunks})
        return self.script


class FakeTTSClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def synthesize(self, *, text: str, voice: str) -> bytes:
        self.calls.append((text, voice))
        return f"audio:{voice}:{text}".encode()


class FakeBlobStore:
    def __init__(self) -> None:
        self.uploaded_audio: list[tuple[str, bytes]] = []
        self.uploaded_covers: list[tuple[str, bytes]] = []

    def upload_audio(self, *, podcast_id: str, audio: bytes) -> str:
        self.uploaded_audio.append((podcast_id, audio))
        return f"https://blob.example.com/podcasts/{podcast_id}/podcast.mp3"

    def upload_cover(self, *, podcast_id: str, cover: bytes) -> str:
        self.uploaded_covers.append((podcast_id, cover))
        return f"https://blob.example.com/podcasts/{podcast_id}/cover.png"


@dataclass(slots=True)
class FakeGenerationDependencies:
    point_reader: FakePointReader
    script_generator: FakeScriptGenerator
    tts_client: FakeTTSClient
    blob_store: FakeBlobStore
    audio_merger: AudioMerger | None
    cover_generator: None = None


class FakePodcastRepository:
    def __init__(self) -> None:
        self.rows: dict[str, PodcastDetail] = {}
        self.created_order: list[str] = []
        self._next_id = 1

    def create(self, label: str) -> PodcastDetail:
        podcast_id = f"podcast-{self._next_id}"
        self._next_id += 1
        podcast = PodcastDetail(id=podcast_id, label=label, status="pending")
        self.rows[podcast_id] = podcast
        self.created_order.append(podcast_id)
        return podcast

    def list(self) -> list[PodcastDetail]:
        return [self._as_list_item(self.rows[podcast_id]) for podcast_id in reversed(self.created_order)]

    def get_by_id(self, podcast_id: str) -> PodcastDetail | None:
        return self.rows.get(podcast_id)

    def mark_running(self, podcast_id: str) -> PodcastDetail:
        current = self.rows[podcast_id]
        assert current.status == "pending"
        updated = current.model_copy(update={"status": "running", "error": None})
        self.rows[podcast_id] = updated
        return updated

    def mark_completed(
        self,
        podcast_id: str,
        *,
        script: PodcastScript,
        audio_url: str | None,
        cover_url: str | None,
    ) -> PodcastDetail:
        current = self.rows[podcast_id]
        assert current.status == "running"
        updated = current.model_copy(
            update={
                "status": "completed",
                "script": script,
                "audio_url": audio_url,
                "cover_url": cover_url,
                "error": None,
            }
        )
        self.rows[podcast_id] = updated
        return updated

    def mark_failed(self, podcast_id: str, *, error: str) -> PodcastDetail:
        current = self.rows[podcast_id]
        assert current.status == "running"
        updated = current.model_copy(update={"status": "failed", "error": error})
        self.rows[podcast_id] = updated
        return updated

    @staticmethod
    def _as_list_item(podcast: PodcastDetail) -> PodcastDetail:
        return PodcastDetail(
            id=podcast.id,
            label=podcast.label,
            status=podcast.status,
            audio_url=podcast.audio_url,
            cover_url=podcast.cover_url,
            script=None,
            error=podcast.error,
        )


def _override_repository(repository: FakePodcastRepository) -> None:
    app.dependency_overrides[get_podcast_repository] = lambda: repository


def _override_generation_dependencies(dependencies: FakeGenerationDependencies) -> None:
    app.dependency_overrides[get_podcast_generation_dependencies] = lambda: dependencies


def _override_point_reader(reader: FakePointReader) -> None:
    app.dependency_overrides[get_point_reader] = lambda: reader


def _clear_overrides() -> None:
    app.dependency_overrides.clear()


def _build_fakes() -> tuple[FakePodcastRepository, FakeGenerationDependencies]:
    reader = FakePointReader(
        {
            "product-updates": [
                VectorPoint(id="1", label="product-updates", text="first chunk"),
                VectorPoint(id="2", label="product-updates", text="second chunk"),
            ],
            "ops": [VectorPoint(id="3", label="ops", text="ops chunk")],
        }
    )
    generation = FakeGenerationDependencies(
        point_reader=reader,
        script_generator=FakeScriptGenerator(
            PodcastScript(
                parts=[
                    PodcastScriptLine(speaker="host_a", text="Intro"),
                    PodcastScriptLine(speaker="host_b", text="Discussion"),
                ]
            )
        ),
        tts_client=FakeTTSClient(),
        blob_store=FakeBlobStore(),
        audio_merger=lambda clips: b"merged:" + b"|".join(clips),
    )
    return FakePodcastRepository(), generation


def test_health_and_universe_endpoints_still_work() -> None:
    reader = FakePointReader(
        {
            "Alpha": [VectorPoint(id="alpha-central", label="Alpha", is_central=True)],
            "Beta": [VectorPoint(id="beta-member", label="Beta", is_central=False)],
        }
    )
    _override_point_reader(reader)
    client = TestClient(app)

    try:
        health_response = client.get("/health")
        universe_response = client.get("/universe")

        assert health_response.status_code == 200
        assert health_response.json() == {"status": "ok"}
        assert universe_response.status_code == 200
        assert universe_response.json() == assemble_universe_graph(reader.points_by_label["Alpha"] + reader.points_by_label["Beta"]).model_dump(mode="json")
    finally:
        _clear_overrides()


def test_create_list_and_detail_podcast_routes_use_background_generation() -> None:
    repository, generation = _build_fakes()
    _override_repository(repository)
    _override_generation_dependencies(generation)
    client = TestClient(app)

    try:
        first_response = client.post("/podcasts", json={"label": "product-updates"})
        second_response = client.post("/podcasts", json={"label": "ops"})
        list_response = client.get("/podcasts")

        first_id = first_response.json()["id"]
        second_id = second_response.json()["id"]

        assert first_response.status_code == 202
        assert first_response.json()["status"] == "pending"
        assert second_response.status_code == 202
        assert second_response.json()["status"] == "pending"

        first_detail = repository.get_by_id(first_id)
        second_detail = repository.get_by_id(second_id)

        assert first_detail is not None
        assert first_detail.status == "completed"
        assert first_detail.audio_url == f"https://blob.example.com/podcasts/{first_id}/podcast.mp3"
        assert first_detail.cover_url == f"https://blob.example.com/podcasts/{first_id}/cover.png"

        assert second_detail is not None
        assert second_detail.status == "completed"

        assert list_response.status_code == 200
        assert [item["id"] for item in list_response.json()] == [second_id, first_id]
        assert list_response.json()[0]["status"] == "completed"
        assert list_response.json()[1]["status"] == "completed"

        detail_response = client.get(f"/podcasts/{first_id}")
        assert detail_response.status_code == 200
        assert detail_response.json()["id"] == first_id
        assert detail_response.json()["label"] == "product-updates"
        assert detail_response.json()["status"] == "completed"
        assert detail_response.json()["script"] == {
            "parts": [
                {"speaker": "host_a", "text": "Intro"},
                {"speaker": "host_b", "text": "Discussion"},
            ]
        }

        missing_response = client.get("/podcasts/missing")
        assert missing_response.status_code == 404
        assert missing_response.json() == {"detail": "podcast not found"}

        assert generation.point_reader.calls == ["product-updates", "ops"]
        assert generation.script_generator.calls == [
            {"label": "product-updates", "chunks": ["first chunk", "second chunk"]},
            {"label": "ops", "chunks": ["ops chunk"]},
        ]
        assert generation.tts_client.calls == [
            ("Intro", "host_a"),
            ("Discussion", "host_b"),
            ("Intro", "host_a"),
            ("Discussion", "host_b"),
        ]
        assert generation.blob_store.uploaded_audio == [
            (first_id, b"merged:audio:host_a:Intro|audio:host_b:Discussion"),
            (second_id, b"merged:audio:host_a:Intro|audio:host_b:Discussion"),
        ]
        assert generation.blob_store.uploaded_covers == [
            (first_id, FALLBACK_COVER_PNG),
            (second_id, FALLBACK_COVER_PNG),
        ]
    finally:
        _clear_overrides()


@pytest.mark.parametrize("label", ["", "   "])
def test_create_podcast_rejects_blank_label(label: str) -> None:
    repository, _ = _build_fakes()
    _override_repository(repository)
    app.dependency_overrides[get_settings] = lambda: Settings(
        database_url="postgresql://placeholder",
        qdrant_url="http://qdrant:6333",
        qdrant_api_key="",
    )
    client = TestClient(app)

    try:
        response = client.post("/podcasts", json={"label": label})

        assert response.status_code == 422
        assert repository.rows == {}
    finally:
        _clear_overrides()


def test_list_podcasts_closes_real_repository_dependency(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    class TrackingRepository:
        def __init__(self) -> None:
            calls.append("repository.__init__")

        def init_db(self) -> None:
            calls.append("repository.init_db")

        def list(self) -> list[PodcastDetail]:
            calls.append("repository.list")
            return []

        def close(self) -> None:
            calls.append("repository.close")

    monkeypatch.setattr("app.main.PodcastRepository", TrackingRepository)
    client = TestClient(app)

    try:
        response = client.get("/podcasts")

        assert response.status_code == 200
        assert response.json() == []
        assert calls == [
            "repository.__init__",
            "repository.init_db",
            "repository.list",
            "repository.close",
        ]
    finally:
        _clear_overrides()


def test_create_podcast_bootstraps_and_closes_separate_request_and_background_repositories(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    captured: dict[str, object] = {}
    repositories: list[TrackingRepository] = []

    class TrackingRepository:
        def __init__(self) -> None:
            self.name = f"repository-{len(repositories) + 1}"
            repositories.append(self)
            calls.append(f"{self.name}.__init__")

        def init_db(self) -> None:
            calls.append(f"{self.name}.init_db")

        def create(self, label: str) -> PodcastDetail:
            calls.append(f"{self.name}.create:{label}")
            podcast = PodcastDetail(id="podcast-real", label=label, status="pending")
            captured["podcast"] = podcast
            return podcast

        def close(self) -> None:
            calls.append(f"{self.name}.close")

    class FakeAsyncQdrantClient:
        def __init__(self, *, url: str, api_key: str | None = None) -> None:
            calls.append(f"qdrant.__init__:{url}:{api_key}")

        async def close(self) -> None:
            calls.append("qdrant.close")

    def fake_point_reader(client: FakeAsyncQdrantClient, collection: str) -> str:
        calls.append(f"point_reader:{collection}")
        assert client is not None
        return "point-reader"

    def fake_build_generation(*, settings: Settings, point_reader: str) -> str:
        calls.append(f"build_generation:{point_reader}:{settings.qdrant_collection}")
        return "generation-deps"

    async def fake_run_generation(
        podcast_id: str,
        repository: TrackingRepository,
        generation: str,
        settings: Settings,
    ) -> None:
        calls.append(f"run_generation:{podcast_id}")
        captured["repository"] = repository
        captured["generation"] = generation
        captured["settings"] = settings

    monkeypatch.setattr("app.main.PodcastRepository", TrackingRepository)
    monkeypatch.setattr("app.main.AsyncQdrantClient", FakeAsyncQdrantClient)
    monkeypatch.setattr("app.main.QdrantPointReader", fake_point_reader)
    monkeypatch.setattr("app.main.build_podcast_generation_dependencies", fake_build_generation)
    monkeypatch.setattr("app.main.run_podcast_generation", fake_run_generation)

    app.dependency_overrides[get_settings] = lambda: Settings(
        database_url="postgresql://placeholder",
        qdrant_url="http://qdrant:6333",
        qdrant_api_key="",
        qdrant_collection="data_provision_points",
    )
    client = TestClient(app)

    try:
        response = client.post("/podcasts", json={"label": "product-updates"})

        assert response.status_code == 202
        assert response.json() == {
            "id": "podcast-real",
            "label": "product-updates",
            "status": "pending",
            "audio_url": None,
            "cover_url": None,
            "script": None,
            "error": None,
        }
        assert len(repositories) == 2
        request_repository, background_repository = repositories
        assert request_repository is not background_repository
        assert captured["repository"] is background_repository
        assert captured["generation"] == "generation-deps"
        assert captured["podcast"] == PodcastDetail(
            id="podcast-real",
            label="product-updates",
            status="pending",
        )
        assert calls[:3] == [
            "repository-1.__init__",
            "repository-1.init_db",
            "repository-1.create:product-updates",
        ]
        assert "qdrant.__init__:http://qdrant:6333:" in calls[3]
        assert calls[4:] == [
            "repository-2.__init__",
            "repository-2.init_db",
            "point_reader:data_provision_points",
            "build_generation:point-reader:data_provision_points",
            "run_generation:podcast-real",
            "repository-2.close",
            "qdrant.close",
            "repository-1.close",
        ]
    finally:
        _clear_overrides()
