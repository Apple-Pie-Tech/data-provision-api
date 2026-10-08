import io
import wave
from types import SimpleNamespace

import httpx
import pytest

from app.podcast_clients import (  # pyright: ignore[reportMissingImports]
    BedrockScriptGenerator,
    FalCoverGenerator,
    PodcastClientError,
    PodcastTimeoutError,
    PollyTTSClient,
    S3PodcastBlobStore,
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


class FakeS3:
    """Stands in for boto3's S3 client, recording the exact kwargs it is sent."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[dict[str, object]] = []

    def put_object(self, **kwargs: object) -> dict[str, object]:
        if self.error is not None:
            raise self.error
        self.calls.append(kwargs)
        return {}

    @property
    def keys(self) -> list[object]:
        return [call["Key"] for call in self.calls]


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


def test_s3_blob_store_uploads_audio_to_the_expected_key() -> None:
    fake = FakeS3()
    store = S3PodcastBlobStore(client=fake, bucket="applepie-podcasts", timeout_seconds=19)

    url = store.upload_audio(podcast_id="podcast-1", audio=b"wav-bytes")

    assert url == (
        "https://applepie-podcasts.s3.us-east-1.amazonaws.com/podcast-1/podcast.wav"
    )
    assert fake.calls == [
        {
            "Bucket": "applepie-podcasts",
            "Key": "podcast-1/podcast.wav",
            "Body": b"wav-bytes",
            "ContentType": "audio/wav",
        }
    ]


def test_s3_blob_store_stores_an_unsigned_url() -> None:
    """E52: the persisted URL must be the canonical one, signed only at read time.

    Storing a presigned URL would put an expiry into the podcasts table and the
    record would rot into a dead link.
    """
    store = S3PodcastBlobStore(client=FakeS3(), bucket="applepie-podcasts")

    url = store.upload_audio(podcast_id="podcast-1", audio=b"wav-bytes")

    assert "X-Amz-Signature" not in url
    assert "?" not in url


@pytest.mark.parametrize(
    ("cover", "expected_key", "expected_content_type"),
    [
        (JPEG_BYTES, "podcast-1/cover.jpg", "image/jpeg"),
        (PNG_BYTES, "podcast-1/cover.png", "image/png"),
        (WEBP_BYTES, "podcast-1/cover.webp", "image/webp"),
        (b"not-an-image", "podcast-1/cover.bin", "application/octet-stream"),
    ],
)
def test_s3_blob_store_names_the_cover_after_its_real_image_type(
    cover: bytes, expected_key: str, expected_content_type: str
) -> None:
    """E46: fal.ai returns JPEG, so `cover.png`/`image/png` was a lie about the bytes."""

    fake = FakeS3()
    store = S3PodcastBlobStore(client=fake, bucket="applepie-podcasts")

    store.upload_cover(podcast_id="podcast-1", cover=cover)

    assert fake.keys == [expected_key]
    assert fake.calls[0]["ContentType"] == expected_content_type


def test_s3_blob_store_sets_a_real_content_type_on_the_request() -> None:
    """The sync Azure SDK silently ignored the bare `content_type=` kwarg.

    Every podcast and cover was therefore stored as application/octet-stream,
    and the old test passed because it only asserted a kwarg had been recorded.
    boto3's spelling is `ContentType`, and the value is what matters.
    """
    fake = FakeS3()
    store = S3PodcastBlobStore(client=fake, bucket="applepie-podcasts")

    store.upload_bytes(blob_name="p1/podcast.wav", data=b"x", content_type="audio/wav")

    assert fake.calls[0]["ContentType"] == "audio/wav"
    assert "content_type" not in fake.calls[0]


def test_s3_blob_store_default_prefix_does_not_repeat_the_bucket_name() -> None:
    """E47: a `podcasts` prefix on a podcasts bucket gave `podcasts/podcasts/<id>/`."""
    fake = FakeS3()
    store = S3PodcastBlobStore(client=fake, bucket="applepie-podcasts")

    store.upload_audio(podcast_id="podcast-1", audio=b"wav-bytes")

    assert fake.keys == ["podcast-1/podcast.wav"]


def test_s3_blob_store_honours_a_custom_object_prefix() -> None:
    fake = FakeS3()
    store = S3PodcastBlobStore(
        client=fake, bucket="applepie-podcasts", object_prefix="episodes"
    )

    store.upload_audio(podcast_id="podcast-1", audio=b"wav-bytes")
    store.upload_cover(podcast_id="podcast-1", cover=PNG_BYTES)

    assert fake.keys == [
        "episodes/podcast-1/podcast.wav",
        "episodes/podcast-1/cover.png",
    ]


def test_s3_blob_store_urls_are_bucket_hosted_not_bucket_pathed() -> None:
    """E21, in its S3 form: the bucket belongs in the host, not as a path segment."""
    store = S3PodcastBlobStore(
        client=FakeS3(), bucket="applepie-podcasts", region="eu-west-1"
    )

    url = store.upload_audio(podcast_id="podcast-1", audio=b"wav-bytes")

    assert url == (
        "https://applepie-podcasts.s3.eu-west-1.amazonaws.com/podcast-1/podcast.wav"
    )


def test_s3_blob_store_wraps_timeouts_as_controlled_errors() -> None:
    request = httpx.Request("PUT", "https://applepie-podcasts.s3.amazonaws.com/x")
    store = S3PodcastBlobStore(
        client=FakeS3(error=httpx.ReadTimeout("timed out", request=request)),
        bucket="applepie-podcasts",
    )

    with pytest.raises(PodcastTimeoutError):
        store.upload_audio(podcast_id="podcast-1", audio=b"wav-bytes")


def test_s3_blob_store_wraps_other_failures_as_controlled_errors() -> None:
    store = S3PodcastBlobStore(
        client=FakeS3(error=RuntimeError("AccessDenied")), bucket="applepie-podcasts"
    )

    with pytest.raises(PodcastClientError, match="S3 upload failed"):
        store.upload_audio(podcast_id="podcast-1", audio=b"wav-bytes")


def test_s3_blob_store_builds_its_client_from_settings(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_client(service: str, **kwargs: object) -> object:
        captured["service"] = service
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("boto3.client", fake_client)

    S3PodcastBlobStore(bucket="applepie-podcasts", region="eu-west-1", timeout_seconds=19)

    assert captured["service"] == "s3"
    assert captured["region_name"] == "eu-west-1"
    assert captured["config"].read_timeout == 19


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
