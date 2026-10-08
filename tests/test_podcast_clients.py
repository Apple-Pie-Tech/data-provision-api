import io
import wave
from types import SimpleNamespace

import httpx
import pytest

from app.podcast_clients import (  # pyright: ignore[reportMissingImports]
    AzurePodcastBlobStore,
    BedrockScriptGenerator,
    FalCoverGenerator,
    PodcastClientError,
    PodcastTimeoutError,
    PollyTTSClient,
)
from app.podcast_schemas import PodcastScript, PodcastScriptLine


# Minimal magic-number headers; enough for content sniffing, not real images.
JPEG_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00"
PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
WEBP_BYTES = b"RIFF\x24\x00\x00\x00WEBPVP8 "


class FakeBedrockRuntime:
    """Stands in for boto3's bedrock-runtime client on the Converse path."""

    def __init__(self, text: str, stop_reason: str = "end_turn") -> None:
        self._text = text
        self._stop_reason = stop_reason
        self.calls: list[dict[str, object]] = []

    def converse(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        return {
            "stopReason": self._stop_reason,
            "output": {"message": {"content": [{"text": self._text}]}},
        }


class FakeAudioStream:
    def __init__(self, pcm: bytes) -> None:
        self._pcm = pcm

    def read(self) -> bytes:
        return self._pcm


class FakePolly:
    def __init__(self, pcm: bytes = b"\x01\x00" * 8, error: Exception | None = None) -> None:
        self._pcm = pcm
        self.error = error
        self.calls: list[dict[str, object]] = []

    def synthesize_speech(self, **kwargs: object) -> dict[str, object]:
        if self.error is not None:
            raise self.error
        self.calls.append(kwargs)
        return {"AudioStream": FakeAudioStream(self._pcm)}


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


def test_bedrock_script_generator_parses_json_and_trims_parts() -> None:
    runtime = FakeBedrockRuntime(
        '{"parts": ['
        '{"speaker": "host_a", "text": "Intro"},'
        '{"speaker": "host_b", "text": "Middle"},'
        '{"speaker": "host_a", "text": "Extra"}]}'
    )
    generator = BedrockScriptGenerator(
        client=runtime, model="test-model", timeout_seconds=42, max_parts=2, max_tokens=256
    )

    script = generator.generate_script(label="product-updates", chunks=["chunk 1", "chunk 2"])

    assert script == PodcastScript(
        parts=[
            PodcastScriptLine(speaker="host_a", text="Intro"),
            PodcastScriptLine(speaker="host_b", text="Middle"),
        ]
    )
    call = runtime.calls[0]
    assert call["modelId"] == "test-model"
    assert call["inferenceConfig"] == {"maxTokens": 256}
    # Converse takes the system prompt in its own argument, never as a message role.
    assert [turn["role"] for turn in call["messages"]] == ["user"]
    assert call["messages"][0]["content"] == [
        {
            "text": "Topic: product-updates\nLimit script parts to at most 2.\n"
            "Chunks:\n1. chunk 1\n2. chunk 2"
        }
    ]
    # Nova has no response_format, so the JSON contract has to be in the prompt.
    assert "single JSON object" in call["system"][0]["text"]


def test_bedrock_script_generator_tolerates_fenced_json() -> None:
    """Nova Pro returns bare JSON; Nova Lite and Micro fence it."""
    runtime = FakeBedrockRuntime('```json\n{"parts": [{"speaker": "host_a", "text": "Hi"}]}\n```')
    generator = BedrockScriptGenerator(client=runtime)

    script = generator.generate_script(label="x", chunks=["c"])

    assert script.parts[0].text == "Hi"


def test_bedrock_script_generator_reports_token_truncation() -> None:
    """A truncated response is invalid JSON; the real cause must not be hidden."""
    runtime = FakeBedrockRuntime('{"parts": [{"speaker": "host', stop_reason="max_tokens")
    generator = BedrockScriptGenerator(client=runtime, max_tokens=8)

    with pytest.raises(PodcastClientError, match="token output limit"):
        generator.generate_script(label="x", chunks=["c"])


def test_bedrock_script_generator_rejects_non_json() -> None:
    generator = BedrockScriptGenerator(client=FakeBedrockRuntime("not json at all"))

    with pytest.raises(PodcastClientError, match="not valid JSON"):
        generator.generate_script(label="x", chunks=["c"])


def test_bedrock_script_generator_rejects_schema_mismatch() -> None:
    generator = BedrockScriptGenerator(client=FakeBedrockRuntime('{"wrong": "shape"}'))

    with pytest.raises(PodcastClientError, match="did not match the schema"):
        generator.generate_script(label="x", chunks=["c"])


def test_polly_tts_client_returns_wav_bytes_and_uses_limited_voice_mapping() -> None:
    """Polly emits headerless PCM, but merge_audio_clips reads clips as WAV.

    Returning raw PCM here would make pydub fail on every podcast, so the client
    has to add the WAV container itself.
    """
    polly = FakePolly(pcm=b"\x01\x00" * 8)
    client = PollyTTSClient(client=polly, timeout_seconds=11)

    audio = client.synthesize(text="hello world", voice="host_b")

    assert audio.startswith(b"RIFF") and b"WAVE" in audio[:16]
    with wave.open(io.BytesIO(audio), "rb") as handle:
        assert handle.getnchannels() == 1
        assert handle.getsampwidth() == 2
        assert handle.getframerate() == 16000
        assert handle.readframes(handle.getnframes()) == b"\x01\x00" * 8

    assert polly.calls == [
        {
            "Text": "hello world",
            "VoiceId": "Matthew",
            "Engine": "generative",
            "OutputFormat": "pcm",
            "SampleRate": "16000",
        }
    ]


def test_polly_tts_client_falls_back_to_host_a_voice_for_unknown_speaker() -> None:
    polly = FakePolly()
    client = PollyTTSClient(client=polly)

    client.synthesize(text="hi", voice="host_zzz")

    assert polly.calls[0]["VoiceId"] == "Ruth"


def test_polly_tts_client_wraps_timeouts_as_controlled_errors() -> None:
    request = httpx.Request("POST", "https://polly.us-east-1.amazonaws.com/v1/speech")
    timeout_error = httpx.ReadTimeout("timed out", request=request)
    client = PollyTTSClient(client=FakePolly(error=timeout_error), timeout_seconds=7)

    with pytest.raises(PodcastTimeoutError):
        client.synthesize(text="hello world")


def test_polly_tts_client_rejects_empty_audio() -> None:
    client = PollyTTSClient(client=FakePolly(pcm=b""))

    with pytest.raises(PodcastClientError, match="no audio"):
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
    blob_client = FakeBlobClient(url="https://account.blob.core.windows.net/podcasts/podcast-1/podcast.wav")
    container_client = FakeContainerClient(blob_client)
    store = AzurePodcastBlobStore(container_client=container_client, timeout_seconds=19)

    url = store.upload_audio(podcast_id="podcast-1", audio=b"wav-bytes")

    assert url == "https://account.blob.core.windows.net/podcasts/podcast-1/podcast.wav"
    assert container_client.calls == ["podcast-1/podcast.wav"]
    assert blob_client.calls == [
        {
            "data": b"wav-bytes",
            "overwrite": True,
            "content_type": "audio/wav",
            "timeout": 19,
        }
    ]


@pytest.mark.parametrize(
    ("cover", "expected_name", "expected_content_type"),
    [
        (JPEG_BYTES, "podcast-1/cover.jpg", "image/jpeg"),
        (PNG_BYTES, "podcast-1/cover.png", "image/png"),
        (WEBP_BYTES, "podcast-1/cover.webp", "image/webp"),
        (b"not-an-image", "podcast-1/cover.bin", "application/octet-stream"),
    ],
)
def test_azure_blob_store_names_the_cover_after_its_real_image_type(
    cover: bytes, expected_name: str, expected_content_type: str
) -> None:
    """E46: fal.ai returns JPEG, so `cover.png`/`image/png` was a lie about the bytes."""

    blob_client = FakeBlobClient(url="https://account.blob.core.windows.net/podcasts/x")
    container_client = FakeContainerClient(blob_client)
    store = AzurePodcastBlobStore(container_client=container_client)

    store.upload_cover(podcast_id="podcast-1", cover=cover)

    assert container_client.calls == [expected_name]
    assert blob_client.calls[0]["content_type"] == expected_content_type


def test_azure_blob_store_fails_without_connection_or_injected_client() -> None:
    with pytest.raises(ValueError):
        AzurePodcastBlobStore()


def test_wrapper_errors_are_controlled_for_fal_url_missing() -> None:
    client = FalCoverGenerator(client=FakeFalClient({"nope": True}))

    with pytest.raises(PodcastClientError):
        client.generate_cover(prompt="podcast cover art")


def test_bedrock_script_generator_builds_its_client_from_settings(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_client(service: str, **kwargs: object) -> object:
        captured["service"] = service
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("boto3.client", fake_client)

    generator = BedrockScriptGenerator(
        region="eu-west-1", model="amazon.nova-pro-v1:0", timeout_seconds=33
    )

    assert generator.model == "amazon.nova-pro-v1:0"
    assert captured["service"] == "bedrock-runtime"
    assert captured["region_name"] == "eu-west-1"
    # The timeout must reach botocore; Converse has no per-call timeout argument.
    assert captured["config"].read_timeout == 33


def test_polly_tts_client_builds_its_client_from_settings(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_client(service: str, **kwargs: object) -> object:
        captured["service"] = service
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("boto3.client", fake_client)

    PollyTTSClient(region="eu-west-1", timeout_seconds=12)

    assert captured["service"] == "polly"
    assert captured["region_name"] == "eu-west-1"
    assert captured["config"].read_timeout == 12


def test_fal_cover_generator_builds_its_client_from_the_configured_key() -> None:
    generator = FalCoverGenerator(api_key="fal-key-from-settings", timeout_seconds=17)

    assert generator.client.key == "fal-key-from-settings"
    assert generator.client.default_timeout == 17


class UrllessBlobClient:
    """A blob client with no `url`, forcing the fallback URL construction."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def upload_blob(self, data: bytes, **kwargs: object) -> None:
        self.calls.append({"data": data, **kwargs})


def test_azure_blob_store_honours_a_custom_blob_prefix() -> None:
    blob_client = FakeBlobClient(url="https://account.blob.core.windows.net/podcasts/x")
    container_client = FakeContainerClient(blob_client)
    store = AzurePodcastBlobStore(container_client=container_client, blob_prefix="episodes")

    store.upload_audio(podcast_id="podcast-1", audio=b"wav-bytes")
    store.upload_cover(podcast_id="podcast-1", cover=PNG_BYTES)

    assert container_client.calls == [
        "episodes/podcast-1/podcast.wav",
        "episodes/podcast-1/cover.png",
    ]


def test_azure_blob_store_fallback_url_uses_the_account_host_not_the_container() -> None:
    """E21: the container name is a path segment, never the storage host."""

    container_client = FakeContainerClient(UrllessBlobClient())
    store = AzurePodcastBlobStore(
        container_client=container_client,
        container_name="podcasts",
        account_name="applepiestories",
    )

    url = store.upload_audio(podcast_id="podcast-1", audio=b"wav-bytes")

    assert url == (
        "https://applepiestories.blob.core.windows.net/podcasts/podcast-1/podcast.wav"
    )


def test_azure_blob_store_derives_the_account_name_from_the_connection_string() -> None:
    container_client = FakeContainerClient(UrllessBlobClient())
    store = AzurePodcastBlobStore(
        container_client=container_client,
        connection_string=(
            "DefaultEndpointsProtocol=https;AccountName=applepiestories;"
            "AccountKey=placeholder;EndpointSuffix=core.windows.net"
        ),
        container_name="podcasts",
    )

    assert store.account_name == "applepiestories"
    assert store.upload_cover(podcast_id="p1", cover=PNG_BYTES).startswith(
        "https://applepiestories.blob.core.windows.net/podcasts/"
    )
