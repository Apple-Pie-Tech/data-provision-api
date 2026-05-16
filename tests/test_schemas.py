from app.schemas import UniverseEdge, UniversePoint, UniverseResponse  # pyright: ignore[reportMissingImports]


def _assert_no_removed_fields(value: object) -> None:
    if isinstance(value, dict):
        assert "title" not in value
        assert "durationSeconds" not in value
        assert "category" not in value
        for nested_value in value.values():
            _assert_no_removed_fields(nested_value)
    elif isinstance(value, list):
        for item in value:
            _assert_no_removed_fields(item)


def test_universe_response_serializes_revised_model() -> None:
    response = UniverseResponse(
        points=[
            UniversePoint(
                id="topic-central",
                label="Topic Cloud",
                audio_url="https://cdn.example.com/audio/topic-central.mp3",
                is_synthetic=True,
                is_central=True,
            ),
            UniversePoint(
                id="topic-child",
                label="Topic Cloud",
                audio_url=None,
                is_synthetic=False,
                is_central=False,
            ),
        ],
        edges=[UniverseEdge(source_id="topic-child", target_id="topic-central")],
    )

    payload = response.model_dump(mode="json")

    assert payload == {
        "points": [
            {
                "id": "topic-central",
                "label": "Topic Cloud",
                "audio_url": "https://cdn.example.com/audio/topic-central.mp3",
                "is_synthetic": True,
                "is_central": True,
            },
            {
                "id": "topic-child",
                "label": "Topic Cloud",
                "audio_url": None,
                "is_synthetic": False,
                "is_central": False,
            },
        ],
        "edges": [{"source_id": "topic-child", "target_id": "topic-central"}],
    }

    _assert_no_removed_fields(payload)
