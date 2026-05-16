from __future__ import annotations

from collections.abc import Sequence

import pytest

from app.podcast_generation import (  # pyright: ignore[reportMissingImports]
    FALLBACK_COVER_PNG,
    PodcastGenerationResult,
    generate_podcast,
)
from app.podcast_schemas import PodcastDetail, PodcastScript, PodcastScriptLine
from app.vector_store import VectorPoint  # pyright: ignore[reportMissingImports]


class FakePodcastRepository:
    def __init__(self) -> None:
        self.rows: dict[str, PodcastDetail] = {}
        self.transitions: list[tuple[str, str]] = []

    def create(self, label: str, podcast_id: str = "podcast-1") -> PodcastDetail:
        podcast = PodcastDetail(id=podcast_id, label=label, status="pending")
        self.rows[podcast_id] = podcast
        self.transitions.append((podcast_id, "pending"))
        return podcast

    def mark_running(self, podcast_id: str) -> PodcastDetail:
        current = self.rows[podcast_id]
        assert current.status == "pending"
        updated = current.model_copy(update={"status": "running", "error": None})
        self.rows[podcast_id] = updated
        self.transitions.append((podcast_id, "running"))
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
        self.transitions.append((podcast_id, "completed"))
        return updated

    def mark_failed(self, podcast_id: str, *, error: str) -> PodcastDetail:
        current = self.rows[podcast_id]
        assert current.status == "running"
        updated = current.model_copy(update={"status": "failed", "error": error})
        self.rows[podcast_id] = updated
        self.transitions.append((podcast_id, "failed"))
        return updated

    def get_by_id(self, podcast_id: str) -> PodcastDetail | None:
        return self.rows.get(podcast_id)


class FakePointReader:
    def __init__(self, points_by_label: dict[str, list[VectorPoint]]) -> None:
        self.points_by_label = points_by_label
        self.calls: list[str] = []

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
    def __init__(self, failure_on_text: str | None = None) -> None:
        self.failure_on_text = failure_on_text
        self.calls: list[tuple[str, str]] = []

    def synthesize(self, *, text: str, voice: str) -> bytes:
        self.calls.append((text, voice))
        if self.failure_on_text == text:
            raise RuntimeError(f"failed on {text}")
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


class FakeCoverGenerator:
    def __init__(self, result: str | None = None, error: Exception | None = None) -> None:
        self.result = result or "https://fal.example.com/generated-cover.png"
        self.error = error
        self.calls: list[str] = []

    def generate_cover(self, *, prompt: str) -> str:
        self.calls.append(prompt)
        if self.error is not None:
            raise self.error
        return self.result


class FakeDownloadResponse:
    def __init__(self, content: bytes) -> None:
        self.content = content

    def raise_for_status(self) -> None:
        return None


class FakeCoverDownloader:
    def __init__(self, content: bytes = b"cover-bytes") -> None:
        self.content = content
        self.calls: list[tuple[str, int]] = []

    def get(self, url: str, **kwargs: object) -> FakeDownloadResponse:
        raw_timeout = kwargs.get("timeout", 0)
        timeout = raw_timeout if isinstance(raw_timeout, int) else 0
        self.calls.append((url, timeout))
        return FakeDownloadResponse(self.content)


def build_points(label: str) -> list[VectorPoint]:
    return [
        VectorPoint(id="1", label=label, text="chunk one"),
        VectorPoint(id="2", label=label, text="chunk two"),
        VectorPoint(id="3", label=label, text="chunk three"),
        VectorPoint(id="4", label=label, text="chunk four"),
    ]


@pytest.mark.asyncio
async def test_generate_podcast_happy_path() -> None:
    repository = FakePodcastRepository()
    created = repository.create("product-updates")
    point_reader = FakePointReader({"product-updates": build_points("product-updates")})
    script_generator = FakeScriptGenerator(
        PodcastScript(
            parts=[
                PodcastScriptLine(speaker="host_a", text="Intro"),
                PodcastScriptLine(speaker="host_b", text="Discussion"),
                PodcastScriptLine(speaker="host_a", text="Wrap"),
            ]
        )
    )
    tts_client = FakeTTSClient()
    blob_store = FakeBlobStore()
    cover_generator = FakeCoverGenerator()
    cover_downloader = FakeCoverDownloader(content=b"downloaded-cover")

    def fake_merge(clips: Sequence[bytes]) -> bytes:
        return b"merged:" + b"|".join(clips)

    result = await generate_podcast(
        created.id,
        repository=repository,
        point_reader=point_reader,
        script_generator=script_generator,
        tts_client=tts_client,
        blob_store=blob_store,
        cover_generator=cover_generator,
        cover_downloader=cover_downloader,
        audio_merger=fake_merge,
        max_chunks=2,
        max_script_parts=2,
        timeout_seconds=15,
    )

    assert isinstance(result, PodcastGenerationResult)
    assert result.cover_used_fallback is False
    assert result.podcast.status == "completed"
    assert result.podcast.script == PodcastScript(
        parts=[
            PodcastScriptLine(speaker="host_a", text="Intro"),
            PodcastScriptLine(speaker="host_b", text="Discussion"),
        ]
    )
    assert result.podcast.audio_url == "https://blob.example.com/podcasts/podcast-1/podcast.mp3"
    assert result.podcast.cover_url == "https://blob.example.com/podcasts/podcast-1/cover.png"
    assert repository.transitions == [
        (created.id, "pending"),
        (created.id, "running"),
        (created.id, "completed"),
    ]
    assert point_reader.calls == ["product-updates"]
    assert script_generator.calls == [
        {"label": "product-updates", "chunks": ["chunk one", "chunk two"]}
    ]
    assert tts_client.calls == [("Intro", "host_a"), ("Discussion", "host_b")]
    assert blob_store.uploaded_audio == [
        (
            "podcast-1",
            b"merged:audio:host_a:Intro|audio:host_b:Discussion",
        )
    ]
    assert blob_store.uploaded_covers == [("podcast-1", b"downloaded-cover")]
    assert cover_downloader.calls == [("https://fal.example.com/generated-cover.png", 15)]


@pytest.mark.asyncio
async def test_generate_podcast_marks_failed_when_no_chunks_are_available() -> None:
    repository = FakePodcastRepository()
    created = repository.create("missing-topic")

    result = await generate_podcast(
        created.id,
        repository=repository,
        point_reader=FakePointReader({}),
        script_generator=FakeScriptGenerator(PodcastScript(parts=[])),
        tts_client=FakeTTSClient(),
        blob_store=FakeBlobStore(),
        cover_generator=FakeCoverGenerator(),
        cover_downloader=FakeCoverDownloader(),
        audio_merger=lambda clips: b"unused",
        max_chunks=3,
        max_script_parts=3,
        timeout_seconds=10,
    )

    assert result.podcast.status == "failed"
    assert result.podcast.error == "no chunks found for label 'missing-topic'"
    saved = repository.get_by_id(created.id)
    assert saved == result.podcast
    assert saved is not None
    assert saved.status != "running"
    assert repository.transitions == [
        (created.id, "pending"),
        (created.id, "running"),
        (created.id, "failed"),
    ]


@pytest.mark.asyncio
async def test_generate_podcast_marks_failed_when_required_generation_step_breaks() -> None:
    repository = FakePodcastRepository()
    created = repository.create("ops")
    point_reader = FakePointReader({"ops": build_points("ops")})

    result = await generate_podcast(
        created.id,
        repository=repository,
        point_reader=point_reader,
        script_generator=FakeScriptGenerator(
            PodcastScript(parts=[PodcastScriptLine(speaker="host_a", text="Intro")])
        ),
        tts_client=FakeTTSClient(failure_on_text="Intro"),
        blob_store=FakeBlobStore(),
        cover_generator=FakeCoverGenerator(),
        cover_downloader=FakeCoverDownloader(),
        audio_merger=lambda clips: b"unused",
        max_chunks=4,
        max_script_parts=4,
        timeout_seconds=10,
    )

    assert result.podcast.status == "failed"
    assert result.podcast.error == "podcast generation failed"
    saved = repository.get_by_id(created.id)
    assert saved is not None
    assert saved.status == "failed"
    assert saved.status != "running"
    assert repository.transitions[-1] == (created.id, "failed")


@pytest.mark.asyncio
async def test_generate_podcast_uses_fallback_cover_without_failing_audio_path() -> None:
    repository = FakePodcastRepository()
    created = repository.create("design")
    point_reader = FakePointReader({"design": build_points("design")})
    blob_store = FakeBlobStore()

    result = await generate_podcast(
        created.id,
        repository=repository,
        point_reader=point_reader,
        script_generator=FakeScriptGenerator(
            PodcastScript(parts=[PodcastScriptLine(speaker="host_a", text="Only line")])
        ),
        tts_client=FakeTTSClient(),
        blob_store=blob_store,
        cover_generator=FakeCoverGenerator(error=RuntimeError("fal is down")),
        cover_downloader=FakeCoverDownloader(),
        audio_merger=lambda clips: b"merged-one",
        max_chunks=4,
        max_script_parts=4,
        timeout_seconds=8,
    )

    assert result.podcast.status == "completed"
    assert result.cover_used_fallback is True
    assert result.podcast.cover_url == "https://blob.example.com/podcasts/podcast-1/cover.png"
    assert blob_store.uploaded_covers == [("podcast-1", FALLBACK_COVER_PNG)]
