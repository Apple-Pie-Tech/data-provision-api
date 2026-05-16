from app.podcast_schemas import (  # pyright: ignore[reportMissingImports]
    PodcastCreateRequest,
    PodcastDetail,
    PodcastListItem,
    PodcastScript,
    PodcastScriptLine,
)


def _assert_no_secret_fields(value: object) -> None:
    secret_field_names = {
        "database_url",
        "openai_api_key",
        "slng_api_key",
        "fal_key",
        "azure_storage_account",
        "azure_storage_container",
        "azure_storage_connection_string",
    }

    if isinstance(value, dict):
        assert secret_field_names.isdisjoint(value)
        for nested_value in value.values():
            _assert_no_secret_fields(nested_value)
    elif isinstance(value, list):
        for item in value:
            _assert_no_secret_fields(item)


def test_podcast_request_and_script_models_serialize_cleanly() -> None:
    request = PodcastCreateRequest(label="product-updates")
    script = PodcastScript(
        parts=[
            PodcastScriptLine(speaker="host_a", text="Welcome back."),
            PodcastScriptLine(speaker="host_b", text="Let's talk product updates."),
        ]
    )

    request_payload = request.model_dump(mode="json")
    script_payload = script.model_dump(mode="json")

    assert request_payload == {"label": "product-updates"}
    assert script_payload == {
        "parts": [
            {"speaker": "host_a", "text": "Welcome back."},
            {"speaker": "host_b", "text": "Let's talk product updates."},
        ]
    }

    _assert_no_secret_fields(request_payload)
    _assert_no_secret_fields(script_payload)


def test_podcast_list_and_detail_models_serialize_without_secrets() -> None:
    item = PodcastListItem(
        id="podcast-123",
        label="product-updates",
        status="completed",
        audio_url="https://cdn.example.com/podcasts/podcast-123/podcast.mp3",
        cover_url="https://cdn.example.com/podcasts/podcast-123/cover.png",
    )
    detail = PodcastDetail(
        id="podcast-123",
        label="product-updates",
        status="completed",
        audio_url="https://cdn.example.com/podcasts/podcast-123/podcast.mp3",
        cover_url="https://cdn.example.com/podcasts/podcast-123/cover.png",
        script=PodcastScript(
            parts=[PodcastScriptLine(speaker="host_a", text="Episode intro.")]
        ),
        error=None,
    )

    item_payload = item.model_dump(mode="json")
    detail_payload = detail.model_dump(mode="json")

    assert item_payload == {
        "id": "podcast-123",
        "label": "product-updates",
        "status": "completed",
        "audio_url": "https://cdn.example.com/podcasts/podcast-123/podcast.mp3",
        "cover_url": "https://cdn.example.com/podcasts/podcast-123/cover.png",
    }
    assert detail_payload == {
        "id": "podcast-123",
        "label": "product-updates",
        "status": "completed",
        "audio_url": "https://cdn.example.com/podcasts/podcast-123/podcast.mp3",
        "cover_url": "https://cdn.example.com/podcasts/podcast-123/cover.png",
        "script": {"parts": [{"speaker": "host_a", "text": "Episode intro."}]},
        "error": None,
    }

    _assert_no_secret_fields(item_payload)
    _assert_no_secret_fields(detail_payload)
