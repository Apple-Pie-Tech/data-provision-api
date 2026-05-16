from types import SimpleNamespace

import httpx
import pytest

from app.podcast_clients import (  # pyright: ignore[reportMissingImports]
    AzurePodcastBlobStore,
    FalCoverGenerator,
    OpenAIScriptGenerator,
    PodcastClientError,
    PodcastTimeoutError,
    SlngTTSClient,
)
from app.podcast_schemas import PodcastScript, PodcastScriptLine


class FakeOpenAICompletions:
    def __init__(self, parsed: PodcastScript) -> None:
        self.parsed = parsed
        self.calls: list[dict[str, object]] = []

    def parse(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(parsed=self.parsed))]
        )


class FakeOpenAIClient:
    def __init__(self, parsed: PodcastScript) -> None:
        self.chat = SimpleNamespace(completions=FakeOpenAICompletions(parsed))


class FakeHTTPResponse:
    def __init__(self, content: bytes) -> None:
        self.content = content

    def raise_for_status(self) -> None:
        return None


class FakeHTTPClient:
    def __init__(self, response: FakeHTTPResponse | None = None, error: Exception | None = None) -> None:
        self.response = response or FakeHTTPResponse(b"audio-bytes")
        self.error = error
        self.calls: list[dict[str, object]] = []

    def post(self, url: str, **kwargs: object) -> FakeHTTPResponse:
        if self.error is not None:
            raise self.error
        self.calls.append({"url": url, **kwargs})
        return self.response


class FakeFalClient:
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

    def run(self, model: str, **kwargs: object) -> object:
        self.calls.append({"model": model, **kwargs})
        return self.result


class FakeBlobClient:
    def __init__(self, url: str) -> None:
        self.url = url
        self.calls: list[dict[str, object]] = []

    def upload_blob(self, data: bytes, **kwargs: object) -> None:
        self.calls.append({"data": data, **kwargs})


class FakeContainerClient:
    def __init__(self, blob_client: FakeBlobClient) -> None:
        self.blob_client = blob_client
        self.calls: list[str] = []

    def get_blob_client(self, blob: str) -> FakeBlobClient:
        self.calls.append(blob)
        return self.blob_client


def test_openai_script_generator_uses_structured_output_and_trims_parts() -> None:
    parsed = PodcastScript(
        parts=[
            PodcastScriptLine(speaker="host_a", text="Intro"),
            PodcastScriptLine(speaker="host_b", text="Middle"),
            PodcastScriptLine(speaker="host_a", text="Extra"),
        ]
    )
    client = FakeOpenAIClient(parsed)
    generator = OpenAIScriptGenerator(client=client, model="test-model", timeout_seconds=42, max_parts=2)

    script = generator.generate_script(label="product-updates", chunks=["chunk 1", "chunk 2"])

    assert script == PodcastScript(
        parts=[
            PodcastScriptLine(speaker="host_a", text="Intro"),
            PodcastScriptLine(speaker="host_b", text="Middle"),
        ]
    )
    assert client.chat.completions.calls == [
        {
            "model": "test-model",
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Write a concise two-host podcast script. Use only host_a and "
                        "host_b. Keep the output bounded and focused."
                    ),
                },
                {
                    "role": "user",
                    "content": "Topic: product-updates\nLimit script parts to at most 2.\nChunks:\n1. chunk 1\n2. chunk 2",
                },
            ],
            "response_format": PodcastScript,
            "timeout": 42,
        }
    ]


def test_slng_tts_client_returns_audio_bytes_and_uses_limited_voice_mapping() -> None:
    http_client = FakeHTTPClient()
    client = SlngTTSClient(client=http_client, timeout_seconds=11)

    audio = client.synthesize(text="hello world", voice="host_b")

    assert audio == b"audio-bytes"
    assert http_client.calls == [
        {
            "url": "https://api.slng.ai/v1/tts/slng/deepgram/aura:2",
            "json": {"text": "hello world", "model": "aura-2-asteria-en"},
            "timeout": 11,
        }
    ]


def test_slng_tts_client_wraps_timeouts_as_controlled_errors() -> None:
    request = httpx.Request("POST", "https://api.slng.ai/v1/tts/slng/deepgram/aura:2")
    timeout_error = httpx.ReadTimeout("timed out", request=request)
    client = SlngTTSClient(client=FakeHTTPClient(error=timeout_error), timeout_seconds=7)

    with pytest.raises(PodcastTimeoutError):
        client.synthesize(text="hello world")


def test_fal_cover_generator_returns_url_from_fal_result() -> None:
    fal_client = FakeFalClient({"images": [{"url": "https://cdn.example.com/cover.png"}]})
    client = FalCoverGenerator(client=fal_client, model="fal-cover-model")

    url = client.generate_cover(prompt="podcast cover art")

    assert url == "https://cdn.example.com/cover.png"
    assert fal_client.calls == [
        {"model": "fal-cover-model", "arguments": {"prompt": "podcast cover art"}}
    ]


def test_azure_blob_store_uploads_bytes_to_expected_path() -> None:
    blob_client = FakeBlobClient(url="https://account.blob.core.windows.net/podcasts/podcast-1/podcast.mp3")
    container_client = FakeContainerClient(blob_client)
    store = AzurePodcastBlobStore(container_client=container_client, timeout_seconds=19)

    url = store.upload_audio(podcast_id="podcast-1", audio=b"mp3-bytes")

    assert url == "https://account.blob.core.windows.net/podcasts/podcast-1/podcast.mp3"
    assert container_client.calls == ["podcasts/podcast-1/podcast.mp3"]
    assert blob_client.calls == [
        {
            "data": b"mp3-bytes",
            "overwrite": True,
            "content_type": "audio/mpeg",
            "timeout": 19,
        }
    ]


def test_azure_blob_store_upload_cover_uses_png_path() -> None:
    blob_client = FakeBlobClient(url="https://account.blob.core.windows.net/podcasts/podcast-1/cover.png")
    container_client = FakeContainerClient(blob_client)
    store = AzurePodcastBlobStore(container_client=container_client)

    url = store.upload_cover(podcast_id="podcast-1", cover=b"png-bytes")

    assert url.endswith("/podcasts/podcast-1/cover.png")
    assert container_client.calls == ["podcasts/podcast-1/cover.png"]


def test_azure_blob_store_fails_without_connection_or_injected_client() -> None:
    with pytest.raises(ValueError):
        AzurePodcastBlobStore()


def test_wrapper_errors_are_controlled_for_fal_url_missing() -> None:
    client = FalCoverGenerator(client=FakeFalClient({"nope": True}))

    with pytest.raises(PodcastClientError):
        client.generate_cover(prompt="podcast cover art")
