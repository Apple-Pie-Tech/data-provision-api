from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class VectorPoint:
    id: str
    label: str
    audio_url: str | None = None
    is_synthetic: bool = False
    is_central: bool = False
    text: Any | None = None
    timestamp: Any | None = None
    user_id: Any | None = None


class AsyncQdrantScrollClient(Protocol):
    async def scroll(
        self,
        collection_name: str,
        *,
        limit: int,
        offset: Any = None,
        with_payload: bool = True,
        with_vectors: bool = False,
    ) -> tuple[list[Any], Any | None]:
        ...


class QdrantPointReader:
    def __init__(
        self,
        client: AsyncQdrantScrollClient,
        collection_name: str,
        *,
        page_size: int = 256,
        max_chunks: int | None = None,
    ) -> None:
        self._client = client
        self._collection_name = collection_name
        self._page_size = page_size
        self._max_chunks = max_chunks

    async def read_points(self) -> list[VectorPoint]:
        points: list[VectorPoint] = []
        offset: Any | None = None

        while True:
            records, next_offset = await self._client.scroll(
                self._collection_name,
                limit=self._page_size,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )

            for record in records:
                point = self._normalize_record(record)
                if point is not None:
                    points.append(point)

            if not records or next_offset is None:
                return points

            offset = next_offset

    async def read_points_for_label(self, label: str | None) -> list[VectorPoint]:
        normalized_label = self._optional_string(label)
        if normalized_label is None:
            return []

        points = await self.read_points()
        filtered_points = [point for point in points if point.label == normalized_label]
        if self._max_chunks is None:
            return filtered_points

        return filtered_points[: self._max_chunks]

    @staticmethod
    def _normalize_record(record: Any) -> VectorPoint | None:
        payload = getattr(record, "payload", None)
        if not isinstance(payload, dict):
            return None

        label = QdrantPointReader._extract_label(payload)
        if label is None:
            # A chunk that story-labeling has not clustered yet. That is the
            # normal state before the first labeling run, so it is skipped
            # rather than treated as an error.
            return None

        is_central = bool(payload.get("is_centroid", False))

        return VectorPoint(
            id=str(getattr(record, "id")),
            label=label,
            audio_url=QdrantPointReader._optional_string(payload.get("audio_url")),
            # Nothing writes `is_synthetic`; the centroid points are the
            # synthetic ones, so `is_centroid` is what the flag was reaching for.
            is_synthetic=bool(payload.get("is_synthetic", is_central)),
            is_central=is_central,
            text=payload.get("text"),
            timestamp=payload.get("timestamp"),
            user_id=payload.get("user_id"),
        )

    @staticmethod
    def _extract_label(payload: dict[str, Any]) -> str | None:
        """Read the cluster theme from either shape story-labeling writes.

        Ordinary story points get the theme merged in nested under
        `clustering`; synthetic centroid points carry the same keys at the top
        level. A reader that checks only one place silently drops the other.
        """

        clustering = payload.get("clustering")
        if isinstance(clustering, dict):
            label = QdrantPointReader._optional_string(clustering.get("theme"))
            if label is not None:
                return label

        return QdrantPointReader._optional_string(payload.get("theme"))

    @staticmethod
    def _optional_string(value: Any) -> str | None:
        if value is None:
            return None

        text = str(value).strip()
        return text or None


__all__ = ["AsyncQdrantScrollClient", "QdrantPointReader", "VectorPoint"]
