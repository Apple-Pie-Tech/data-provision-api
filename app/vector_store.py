from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol


# ListVectors returns at most 1000 vectors per page.
MAX_VECTORS_PER_LIST = 1000

# story-labeling-api writes the clustering result as flat `clustering_*` keys
# because S3 Vectors rejects a nested metadata object. `is_centroid` is the one
# clustering key it leaves unprefixed.
CLUSTERING_METADATA_PREFIX = "clustering_"


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


class S3VectorsListClient(Protocol):
    def list_vectors(self, **kwargs: Any) -> dict[str, Any]: ...


class S3VectorsPointReader:
    """Reads the universe graph's points out of an S3 Vectors index.

    Only metadata is requested: this service never needs the vectors
    themselves, and they are by far the largest part of a response.
    """

    def __init__(
        self,
        client: S3VectorsListClient,
        vector_bucket: str,
        index_name: str,
        *,
        page_size: int = 500,
        max_chunks: int | None = None,
    ) -> None:
        self._client = client
        self._vector_bucket = vector_bucket
        self._index_name = index_name
        self._page_size = min(page_size, MAX_VECTORS_PER_LIST)
        self._max_chunks = max_chunks

    async def read_points(self) -> list[VectorPoint]:
        points: list[VectorPoint] = []
        next_token: str | None = None

        while True:
            kwargs: dict[str, Any] = {
                "vectorBucketName": self._vector_bucket,
                "indexName": self._index_name,
                "returnData": False,
                "returnMetadata": True,
                "maxResults": self._page_size,
            }
            if next_token:
                kwargs["nextToken"] = next_token

            page = await asyncio.to_thread(self._client.list_vectors, **kwargs)

            for record in page.get("vectors") or []:
                point = self._normalize_record(record)
                if point is not None:
                    points.append(point)

            next_token = page.get("nextToken")
            if not next_token:
                return points

    async def read_points_for_label(self, label: str | None) -> list[VectorPoint]:
        """Source chunks for one cluster label, for podcast script generation.

        Synthetic centroids carry their cluster's label but no chunk text, so
        they are excluded: ``max_chunks`` is a budget of usable chunks, and a
        centroid taking one of those slots just makes the script shorter.
        """
        normalized_label = self._optional_string(label)
        if normalized_label is None:
            return []

        points = await self.read_points()
        filtered_points = [
            point
            for point in points
            if point.label == normalized_label
            and not point.is_central
            and self._optional_string(point.text) is not None
        ]
        if self._max_chunks is None:
            return filtered_points

        return filtered_points[: self._max_chunks]

    @staticmethod
    def _normalize_record(record: Any) -> VectorPoint | None:
        if not isinstance(record, Mapping):
            return None

        key = record.get("key")
        metadata = record.get("metadata")
        if not isinstance(key, str) or not key:
            return None
        if not isinstance(metadata, Mapping):
            return None

        label = S3VectorsPointReader._extract_label(metadata)
        if label is None:
            # A chunk that story-labeling has not clustered yet. That is the
            # normal state before the first labeling run, so it is skipped
            # rather than treated as an error.
            return None

        is_central = bool(metadata.get("is_centroid", False))

        return VectorPoint(
            id=key,
            label=label,
            audio_url=S3VectorsPointReader._optional_string(metadata.get("audio_url")),
            # Nothing writes `is_synthetic`; the centroid points are the
            # synthetic ones, so `is_centroid` is what the flag was reaching for.
            is_synthetic=bool(metadata.get("is_synthetic", is_central)),
            is_central=is_central,
            text=metadata.get("text"),
            timestamp=metadata.get("timestamp"),
            user_id=metadata.get("user_id"),
        )

    @staticmethod
    def _extract_label(metadata: Mapping[str, Any]) -> str | None:
        """Read the cluster theme.

        Ordinary points and synthetic centroids now carry it under the same flat
        key, so there is one place to look. Under Qdrant the theme was nested for
        one and top-level for the other, and a reader that checked only one place
        silently dropped the other -- which is how /universe came back empty (E7).
        """

        return S3VectorsPointReader._optional_string(
            metadata.get(f"{CLUSTERING_METADATA_PREFIX}theme")
        )

    @staticmethod
    def _optional_string(value: Any) -> str | None:
        if value is None:
            return None

        text = str(value).strip()
        return text or None


__all__ = [
    "CLUSTERING_METADATA_PREFIX",
    "MAX_VECTORS_PER_LIST",
    "S3VectorsListClient",
    "S3VectorsPointReader",
    "VectorPoint",
]
