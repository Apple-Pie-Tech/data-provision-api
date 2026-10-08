"""Reader tests written against the *real* producer payload shapes.

Two services write into the shared `apple_pie_story_chunks` collection and
neither of them writes a top-level `label`:

* `data-ingestion/app/vector_store.py:200-221` writes the raw chunk payload
  (`input_id`, `user_id`, `timestamp`, `chunk_index`, `text`, `source`,
  `embedding_model`, `semantic_chunking`, optional `audio_url`).
* `story-labeling-api/app/vector_store.py:141-146` merges the cluster theme in
  **nested** under `clustering`, while `app/service.py:151-167` upserts synthetic
  centroid points whose identical keys sit at the **top level** alongside
  `is_centroid`.

The fixtures below reproduce those payloads verbatim.
"""

from types import SimpleNamespace

import pytest

from app.vector_store import QdrantPointReader, VectorPoint  # pyright: ignore[reportMissingImports]


COLLECTION = "apple_pie_story_chunks"
SCOPE = "full_collection_original_embedding_space"


def ingested_chunk_payload(
    *,
    text: str = "a memory",
    user_id: str = "user-1",
    audio_url: str | None = None,
) -> dict[str, object]:
    """Exactly what data-ingestion writes, before labeling has ever run."""

    payload: dict[str, object] = {
        "input_id": "input-1",
        "user_id": user_id,
        "timestamp": "2026-01-01T00:00:00Z",
        "chunk_index": 0,
        "text": text,
        "source": "text",
        "embedding_model": "text-embedding-3-small",
        "semantic_chunking": {"break_threshold": 0.82, "overlap_sentences": 1},
    }
    if audio_url is not None:
        payload["audio_url"] = audio_url
    return payload


def labelled_chunk_payload(theme: str, **kwargs: object) -> dict[str, object]:
    """An ingested chunk after story-labeling merged its `clustering` payload."""

    payload = ingested_chunk_payload(**kwargs)  # type: ignore[arg-type]
    payload["clustering"] = {
        "algorithm": "hdbscan",
        "scope": SCOPE,
        "cluster_id": 0,
        "theme": theme,
        "description": "a description",
        "is_noise": False,
    }
    return payload


def noise_chunk_payload(**kwargs: object) -> dict[str, object]:
    payload = ingested_chunk_payload(**kwargs)  # type: ignore[arg-type]
    payload["clustering"] = {
        "algorithm": "hdbscan",
        "scope": SCOPE,
        "cluster_id": -1,
        "theme": "Noise / Outliers",
        "description": None,
        "is_noise": True,
    }
    return payload


def centroid_payload(theme: str, *, cluster_id: int = 0) -> dict[str, object]:
    """Synthetic centroid point — every key top level, no `clustering` nesting."""

    return {
        "is_centroid": True,
        "algorithm": "hdbscan",
        "scope": SCOPE,
        "cluster_id": cluster_id,
        "theme": theme,
        "description": "a description",
        "is_noise": False,
    }


class FakeQdrantClient:
    def __init__(self, pages: list[tuple[list[SimpleNamespace], object | None]]) -> None:
        self.pages = pages
        self.calls: list[dict[str, object]] = []

    async def scroll(  # type: ignore[override]
        self,
        collection_name: str,
        *,
        limit: int,
        offset: object | None = None,
        with_payload: bool = True,
        with_vectors: bool = False,
    ) -> tuple[list[SimpleNamespace], object | None]:
        self.calls.append(
            {
                "collection_name": collection_name,
                "limit": limit,
                "offset": offset,
                "with_payload": with_payload,
                "with_vectors": with_vectors,
            }
        )

        if not self.pages:
            return [], None

        return self.pages.pop(0)


@pytest.mark.asyncio
async def test_reader_reads_the_label_from_the_nested_clustering_payload() -> None:
    client = FakeQdrantClient(
        pages=[
            (
                [
                    SimpleNamespace(
                        id=101,
                        payload=labelled_chunk_payload(
                            "Topic Cloud",
                            text="hello",
                            audio_url="https://cdn.example.com/audio/101.wav",
                        ),
                    )
                ],
                None,
            )
        ]
    )
    reader = QdrantPointReader(client, COLLECTION)

    points = await reader.read_points()

    assert points == [
        VectorPoint(
            id="101",
            label="Topic Cloud",
            audio_url="https://cdn.example.com/audio/101.wav",
            is_synthetic=False,
            is_central=False,
            text="hello",
            timestamp="2026-01-01T00:00:00Z",
            user_id="user-1",
        )
    ]
    assert client.calls == [
        {
            "collection_name": COLLECTION,
            "limit": 256,
            "offset": None,
            "with_payload": True,
            "with_vectors": False,
        }
    ]


@pytest.mark.asyncio
async def test_reader_reads_centroid_points_from_their_top_level_theme() -> None:
    client = FakeQdrantClient(
        pages=[
            (
                [
                    SimpleNamespace(
                        id="centroid:hdbscan:0",
                        payload=centroid_payload("Topic Cloud"),
                    )
                ],
                None,
            )
        ]
    )
    reader = QdrantPointReader(client, COLLECTION)

    points = await reader.read_points()

    assert points == [
        VectorPoint(
            id="centroid:hdbscan:0",
            label="Topic Cloud",
            audio_url=None,
            is_synthetic=True,
            is_central=True,
            text=None,
            timestamp=None,
            user_id=None,
        )
    ]


@pytest.mark.asyncio
async def test_reader_skips_chunks_that_labeling_has_not_touched_yet() -> None:
    client = FakeQdrantClient(
        pages=[
            (
                [
                    SimpleNamespace(id="unlabelled", payload=ingested_chunk_payload()),
                    SimpleNamespace(id="no-payload", payload=None),
                    SimpleNamespace(id="blank-theme", payload={"clustering": {"theme": "   "}}),
                    SimpleNamespace(id="null-theme", payload={"clustering": {"theme": None}}),
                    SimpleNamespace(id="junk-clustering", payload={"clustering": "nonsense"}),
                    SimpleNamespace(
                        id="labelled",
                        payload=labelled_chunk_payload("Keep Me", text="keep"),
                    ),
                ],
                None,
            )
        ]
    )
    reader = QdrantPointReader(client, COLLECTION)

    points = await reader.read_points()

    assert [point.id for point in points] == ["labelled"]
    assert points[0].label == "Keep Me"


@pytest.mark.asyncio
async def test_reader_surfaces_noise_points_under_their_producer_theme() -> None:
    client = FakeQdrantClient(
        pages=[([SimpleNamespace(id="noise-1", payload=noise_chunk_payload())], None)]
    )
    reader = QdrantPointReader(client, COLLECTION)

    points = await reader.read_points()

    assert [(point.id, point.label) for point in points] == [("noise-1", "Noise / Outliers")]


@pytest.mark.asyncio
async def test_reader_returns_empty_list_for_empty_collection() -> None:
    client = FakeQdrantClient(pages=[([], None)])
    reader = QdrantPointReader(client, COLLECTION)

    points = await reader.read_points()

    assert points == []


@pytest.mark.asyncio
async def test_reader_filters_points_by_label_and_enforces_max_chunks() -> None:
    client = FakeQdrantClient(
        pages=[
            (
                [
                    SimpleNamespace(id="one", payload=labelled_chunk_payload("Topic A", text="1")),
                    SimpleNamespace(id="two", payload=labelled_chunk_payload("Topic B", text="2")),
                    SimpleNamespace(id="three", payload=labelled_chunk_payload("Topic A", text="3")),
                    SimpleNamespace(id="four", payload=labelled_chunk_payload("Topic A", text="4")),
                    SimpleNamespace(id="raw", payload=ingested_chunk_payload(text="unlabelled")),
                ],
                None,
            )
        ]
    )
    reader = QdrantPointReader(client, COLLECTION, max_chunks=2)

    points = await reader.read_points_for_label("  Topic A  ")

    assert [(point.id, point.text) for point in points] == [("one", "1"), ("three", "3")]
    assert client.calls == [
        {
            "collection_name": COLLECTION,
            "limit": 256,
            "offset": None,
            "with_payload": True,
            "with_vectors": False,
        }
    ]


@pytest.mark.asyncio
async def test_reader_returns_empty_list_for_missing_or_blank_label() -> None:
    client = FakeQdrantClient(pages=[([], None)])
    reader = QdrantPointReader(client, COLLECTION, max_chunks=3)

    assert await reader.read_points_for_label(None) == []
    assert await reader.read_points_for_label("   ") == []
    assert client.calls == []
