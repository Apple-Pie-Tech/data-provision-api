from app.universe import assemble_universe_graph  # pyright: ignore[reportMissingImports]
from app.vector_store import VectorPoint  # pyright: ignore[reportMissingImports]


def test_assemble_universe_graph_builds_member_and_central_edges() -> None:
    points = [
        VectorPoint(id="beta-member", label="Beta", is_central=False),
        VectorPoint(id="alpha-central", label="Alpha", is_central=True),
        VectorPoint(id="beta-central", label="Beta", is_central=True),
        VectorPoint(id="alpha-member", label="Alpha", is_central=False),
    ]

    response = assemble_universe_graph(points)

    assert response.model_dump() == {
        "points": [
            {
                "id": "beta-member",
                "label": "Beta",
                "audio_url": None,
                "is_synthetic": False,
                "is_central": False,
            },
            {
                "id": "alpha-central",
                "label": "Alpha",
                "audio_url": None,
                "is_synthetic": False,
                "is_central": True,
            },
            {
                "id": "beta-central",
                "label": "Beta",
                "audio_url": None,
                "is_synthetic": False,
                "is_central": True,
            },
            {
                "id": "alpha-member",
                "label": "Alpha",
                "audio_url": None,
                "is_synthetic": False,
                "is_central": False,
            },
        ],
        "edges": [
            {"source_id": "alpha-member", "target_id": "alpha-central"},
            {"source_id": "beta-member", "target_id": "beta-central"},
            {"source_id": "alpha-central", "target_id": "beta-central"},
        ],
    }


def test_assemble_universe_graph_skips_member_edges_for_missing_central_label() -> None:
    points = [
        VectorPoint(id="orphan-member", label="Orphan", is_central=False),
        VectorPoint(id="kept-central", label="Kept", is_central=True),
        VectorPoint(id="kept-member", label="Kept", is_central=False),
    ]

    response = assemble_universe_graph(points)

    assert response.model_dump()["edges"] == [
        {"source_id": "kept-member", "target_id": "kept-central"}
    ]


def test_assemble_universe_graph_uses_stable_primary_central_for_duplicates() -> None:
    points = [
        VectorPoint(id="topic-member", label="Topic", is_central=False),
        VectorPoint(id="topic-z", label="Topic", is_central=True),
        VectorPoint(id="topic-a", label="Topic", is_central=True),
    ]

    response = assemble_universe_graph(points)

    assert response.model_dump()["edges"] == [
        {"source_id": "topic-member", "target_id": "topic-a"},
        {"source_id": "topic-a", "target_id": "topic-z"},
    ]
