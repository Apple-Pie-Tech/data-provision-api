"""Reader tests written against the *real* producer metadata shapes.

Two services write into the shared S3 Vectors index and neither writes a
top-level `label`:

* `data-ingestion/app/vector_store.py` writes the raw chunk metadata
  (`input_id`, `user_id`, `timestamp`, `chunk_index`, `text`, `source`,
  `embedding_model`, `semantic_chunking_*`, optional `audio_url`).
* `story-labeling-api` merges the cluster theme in as flat `clustering_*` keys,
  and writes synthetic centroid points carrying the same keys plus an
  unprefixed `is_centroid`.

S3 Vectors rejects nested metadata objects, so both producers are flat and the
theme lives under one key for both -- which is what removes the reader's old
two-shape lookup, and with it the class of bug behind the empty /universe (E7).

The fixtures below reproduce those payloads verbatim.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.vector_store import (  # pyright: ignore[reportMissingImports]
    MAX_VECTORS_PER_LIST,
    S3VectorsPointReader,
    VectorPoint,
)


BUCKET = "applepie-vectors"
INDEX = "apple-pie-story-chunks"
SCOPE = "full_collection_original_embedding_space"


def ingested_chunk_metadata(
    *,
    text: str = "a memory",
    user_id: str = "user-1",
    audio_url: str | None = None,
) -> dict[str, Any]:
    """Exactly what data-ingestion writes, before labeling has ever run."""

    metadata: dict[str, Any] = {
        "input_id": "input-1",
        "user_id": user_id,
        "timestamp": "2026-01-01T00:00:00Z",
        "chunk_index": 0,
        "text": text,
        "source": "text",
        "embedding_model": "cohere.embed-v4:0",
        "semantic_chunking_break_threshold": 0.82,
        "semantic_chunking_overlap_sentences": 1,
    }
    if audio_url is not None:
        metadata["audio_url"] = audio_url
    return metadata


def labelled_chunk_metadata(theme: str, **kwargs: Any) -> dict[str, Any]:
    """An ingested chunk after story-labeling wrote its clustering keys."""

    metadata = ingested_chunk_metadata(**kwargs)
    metadata.update(
        {
            "clustering_algorithm": "hdbscan",
            "clustering_scope": SCOPE,
            "clustering_cluster_id": 0,
            "clustering_theme": theme,
            "clustering_description": "a description",
            "clustering_is_noise": False,
        }
    )
    return metadata


def noise_chunk_metadata(**kwargs: Any) -> dict[str, Any]:
    metadata = ingested_chunk_metadata(**kwargs)
    metadata.update(
        {
            "clustering_algorithm": "hdbscan",
            "clustering_scope": SCOPE,
            "clustering_cluster_id": -1,
            "clustering_theme": "Noise / Outliers",
            "clustering_is_noise": True,
        }
    )
    return metadata


def centroid_metadata(theme: str, *, cluster_id: int = 0) -> dict[str, Any]:
    """A synthetic centroid point, as story-labeling-api writes it."""

    return {
        "is_centroid": True,
        "clustering_algorithm": "hdbscan",
        "clustering_scope": SCOPE,
        "clustering_centroid_key": f"centroid:hdbscan:{cluster_id}",
        "clustering_cluster_id": cluster_id,
        "clustering_theme": theme,
        "clustering_description": "a description",
        "clustering_is_noise": False,
    }


def record(key: str, metadata: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {"key": key}
    if metadata is not None:
        out["metadata"] = metadata
    return out


class FakeS3Vectors:
    def __init__(self, pages: list[list[dict[str, Any]]]) -> None:
        self.pages = pages
        self.calls: list[dict[str, Any]] = []

    def list_vectors(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        index = int(kwargs.get("nextToken", "0"))
        if index >= len(self.pages):
            return {"vectors": []}
        page: dict[str, Any] = {"vectors": self.pages[index]}
        if index + 1 < len(self.pages):
            page["nextToken"] = str(index + 1)
        return page


def make_reader(
    pages: list[list[dict[str, Any]]],
    **kwargs: Any,
) -> tuple[S3VectorsPointReader, FakeS3Vectors]:
    client = FakeS3Vectors(pages)
    return S3VectorsPointReader(client, BUCKET, INDEX, **kwargs), client


@pytest.mark.asyncio
async def test_reader_reads_the_label_from_the_flat_clustering_key() -> None:
    reader, client = make_reader(
        [
            [
                record(
                    "101",
                    labelled_chunk_metadata(
                        "Topic Cloud",
                        text="hello",
                        audio_url="https://applepie-audio.s3.us-east-1.amazonaws.com/a.wav",
                    ),
                )
            ]
        ]
    )

    points = await reader.read_points()

    assert points == [
        VectorPoint(
            id="101",
            label="Topic Cloud",
            audio_url="https://applepie-audio.s3.us-east-1.amazonaws.com/a.wav",
            is_synthetic=False,
            is_central=False,
            text="hello",
            timestamp="2026-01-01T00:00:00Z",
            user_id="user-1",
        )
    ]
    assert client.calls == [
        {
            "vectorBucketName": BUCKET,
            "indexName": INDEX,
            "returnData": False,
            "returnMetadata": True,
            "maxResults": 500,
        }
    ]


@pytest.mark.asyncio
async def test_reader_never_asks_for_vector_data() -> None:
    """The graph needs metadata only, and the vectors dominate the payload size."""
    reader, client = make_reader([[record("101", labelled_chunk_metadata("Topic"))]])

    await reader.read_points()

    assert all(call["returnData"] is False for call in client.calls)


@pytest.mark.asyncio
async def test_reader_reads_centroid_points_from_the_same_key() -> None:
    reader, _ = make_reader([[record("centroid-key-0", centroid_metadata("Topic Cloud"))]])

    points = await reader.read_points()

    assert points == [
        VectorPoint(
            id="centroid-key-0",
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
async def test_reader_paginates_until_the_token_runs_out() -> None:
    reader, client = make_reader(
        [
            [record("one", labelled_chunk_metadata("Topic A", text="1"))],
            [record("two", labelled_chunk_metadata("Topic A", text="2"))],
            [record("three", labelled_chunk_metadata("Topic A", text="3"))],
        ]
    )

    points = await reader.read_points()

    assert [point.id for point in points] == ["one", "two", "three"]
    assert len(client.calls) == 3
    assert [call.get("nextToken") for call in client.calls] == [None, "1", "2"]


@pytest.mark.asyncio
async def test_reader_skips_chunks_that_labeling_has_not_touched_yet() -> None:
    reader, _ = make_reader(
        [
            [
                record("unlabelled", ingested_chunk_metadata()),
                record("no-metadata", None),
                record("blank-theme", {"clustering_theme": "   "}),
                record("null-theme", {"clustering_theme": None}),
                record("labelled", labelled_chunk_metadata("Keep Me", text="keep")),
            ]
        ]
    )

    points = await reader.read_points()

    assert [point.id for point in points] == ["labelled"]
    assert points[0].label == "Keep Me"


@pytest.mark.asyncio
async def test_reader_ignores_a_record_without_a_usable_key() -> None:
    reader, _ = make_reader(
        [
            [
                {"metadata": labelled_chunk_metadata("Topic")},
                record("", labelled_chunk_metadata("Topic")),
                record("good", labelled_chunk_metadata("Topic")),
            ]
        ]
    )

    points = await reader.read_points()

    assert [point.id for point in points] == ["good"]


@pytest.mark.asyncio
async def test_reader_surfaces_noise_points_under_their_producer_theme() -> None:
    reader, _ = make_reader([[record("noise-1", noise_chunk_metadata())]])

    points = await reader.read_points()

    assert [(point.id, point.label) for point in points] == [("noise-1", "Noise / Outliers")]


@pytest.mark.asyncio
async def test_reader_returns_empty_list_for_an_empty_index() -> None:
    reader, _ = make_reader([[]])

    assert await reader.read_points() == []


@pytest.mark.asyncio
async def test_reader_filters_points_by_label_and_enforces_max_chunks() -> None:
    reader, _ = make_reader(
        [
            [
                record("one", labelled_chunk_metadata("Topic A", text="1")),
                record("two", labelled_chunk_metadata("Topic B", text="2")),
                record("three", labelled_chunk_metadata("Topic A", text="3")),
                record("four", labelled_chunk_metadata("Topic A", text="4")),
                record("raw", ingested_chunk_metadata(text="unlabelled")),
            ]
        ],
        max_chunks=2,
    )

    points = await reader.read_points_for_label("  Topic A  ")

    assert [(point.id, point.text) for point in points] == [("one", "1"), ("three", "3")]


@pytest.mark.asyncio
async def test_reader_excludes_centroids_from_a_labels_source_chunks() -> None:
    """A centroid shares its cluster's label but has no chunk text.

    Found by the live run: `read_points_for_label` returned the centroid
    alongside the real chunks, and because max_chunks is applied before
    podcast_generation filters textless points, the centroid consumed one of
    the budgeted slots and shortened the script.
    """
    reader, _ = make_reader(
        [
            [
                record("centroid-key-0", centroid_metadata("Topic A")),
                record("one", labelled_chunk_metadata("Topic A", text="1")),
                record("two", labelled_chunk_metadata("Topic A", text="2")),
            ]
        ],
        max_chunks=2,
    )

    points = await reader.read_points_for_label("Topic A")

    assert [point.id for point in points] == ["one", "two"]
    assert all(point.text for point in points)


@pytest.mark.asyncio
async def test_reader_returns_empty_list_for_missing_or_blank_label() -> None:
    reader, client = make_reader([[]], max_chunks=3)

    assert await reader.read_points_for_label(None) == []
    assert await reader.read_points_for_label("   ") == []
    assert client.calls == []


def test_page_size_cannot_exceed_the_api_limit() -> None:
    """ListVectors returns at most 1000 per page; a larger setting must be clamped."""
    reader, _ = make_reader([[]], page_size=5000)

    assert reader._page_size == MAX_VECTORS_PER_LIST
