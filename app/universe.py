from __future__ import annotations

from collections import defaultdict

from .schemas import UniverseEdge, UniversePoint, UniverseResponse  # pyright: ignore[reportMissingImports]
from .vector_store import VectorPoint  # pyright: ignore[reportMissingImports]


def assemble_universe_graph(points: list[VectorPoint]) -> UniverseResponse:
    grouped_points: dict[str, list[VectorPoint]] = defaultdict(list)
    for point in points:
        grouped_points[point.label].append(point)

    response_points = [
        UniversePoint(
            id=point.id,
            label=point.label,
            audio_url=point.audio_url,
            is_synthetic=point.is_synthetic,
            is_central=point.is_central,
        )
        for point in points
    ]

    central_ids = sorted({point.id for point in points if point.is_central})

    edges: list[UniverseEdge] = []

    label_central_ids: dict[str, str] = {}
    for label in sorted(grouped_points):
        label_points = grouped_points[label]
        label_centrals = sorted((point for point in label_points if point.is_central), key=lambda item: item.id)
        if label_centrals:
            label_central_ids[label] = label_centrals[0].id

    for label in sorted(grouped_points):
        label_points = grouped_points[label]
        central_id = label_central_ids.get(label)
        if central_id is None:
            continue

        for point in label_points:
            if point.is_central:
                continue
            edges.append(UniverseEdge(source_id=point.id, target_id=central_id))

    for index, source_id in enumerate(central_ids):
        for target_id in central_ids[index + 1 :]:
            edges.append(UniverseEdge(source_id=source_id, target_id=target_id))

    return UniverseResponse(points=response_points, edges=edges)


__all__ = ["assemble_universe_graph"]
