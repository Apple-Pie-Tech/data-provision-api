from types import SimpleNamespace

import pytest

from app.vector_store import QdrantPointReader, VectorPoint  # pyright: ignore[reportMissingImports]


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
async def test_reader_normalizes_valid_points() -> None:
    client = FakeQdrantClient(
        pages=[
            (
                [
                    SimpleNamespace(
                        id=101,
                        payload={
                            "label": "Topic Cloud",
                            "audio_url": "https://cdn.example.com/audio/101.mp3",
                            "is_synthetic": True,
                            "is_central": True,
                            "text": "hello",
                            "timestamp": "2026-01-01T00:00:00Z",
                            "user_id": "user-1",
                        },
                    ),
                    SimpleNamespace(
                        id="child-1",
                        payload={
                            "label": "Topic Cloud",
                            "audio_url": None,
                            "is_synthetic": False,
                            "is_central": False,
                        },
                    ),
                ],
                None,
            )
        ]
    )
    reader = QdrantPointReader(client, "data_provision_points")

    points = await reader.read_points()

    assert points == [
        VectorPoint(
            id="101",
            label="Topic Cloud",
            audio_url="https://cdn.example.com/audio/101.mp3",
            is_synthetic=True,
            is_central=True,
            text="hello",
            timestamp="2026-01-01T00:00:00Z",
            user_id="user-1",
        ),
        VectorPoint(
            id="child-1",
            label="Topic Cloud",
            audio_url=None,
            is_synthetic=False,
            is_central=False,
        ),
    ]

    assert client.calls == [
        {
            "collection_name": "data_provision_points",
            "limit": 256,
            "offset": None,
            "with_payload": True,
            "with_vectors": False,
        }
    ]


@pytest.mark.asyncio
async def test_reader_skips_missing_or_blank_labels() -> None:
    client = FakeQdrantClient(
        pages=[
            (
                [
                    SimpleNamespace(id="missing", payload={"audio_url": "x"}),
                    SimpleNamespace(id="blank", payload={"label": "   "}),
                    SimpleNamespace(
                        id="valid",
                        payload={
                            "label": "Keep Me",
                            "is_synthetic": False,
                            "is_central": True,
                        },
                    ),
                ],
                None,
            )
        ]
    )
    reader = QdrantPointReader(client, "data_provision_points")

    points = await reader.read_points()

    assert points == [
        VectorPoint(
            id="valid",
            label="Keep Me",
            audio_url=None,
            is_synthetic=False,
            is_central=True,
        )
    ]


@pytest.mark.asyncio
async def test_reader_returns_empty_list_for_empty_collection() -> None:
    client = FakeQdrantClient(pages=[([], None)])
    reader = QdrantPointReader(client, "data_provision_points")

    points = await reader.read_points()

    assert points == []
