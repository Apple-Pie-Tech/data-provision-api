from __future__ import annotations

import io
import json
import wave
from dataclasses import dataclass
from importlib import import_module
from typing import Any, Protocol

import httpx
from botocore.exceptions import ConnectTimeoutError, ReadTimeoutError

from app.podcast_schemas import PodcastScript


# Nova Pro, not GPT or Claude: every GPT-6/GPT-5.6 and Claude 5.x model is listed in
# this account's Bedrock catalogue but returns "not available for this account" when
# invoked. Nova Pro was verified to return bare, parseable JSON for this prompt.
DEFAULT_BEDROCK_SCRIPT_MODEL = "amazon.nova-pro-v1:0"
# A truncated response is invalid JSON, so this is a correctness setting as much as a
# cost one. 12 script parts need well under this.
DEFAULT_BEDROCK_SCRIPT_MAX_TOKENS = 2048
DEFAULT_FAL_MODEL = "fal-ai/flux/schnell"
DEFAULT_AUDIO_VOICE = "host_a"
DEFAULT_HOST_B_VOICE = "host_b"
# Polly generative voices, confirmed available in us-east-1. One female, one male, so
# the two hosts are distinguishable.
DEFAULT_AUDIO_VOICE_MODEL = "Ruth"
DEFAULT_HOST_B_VOICE_MODEL = "Matthew"
DEFAULT_POLLY_ENGINE = "generative"
# Polly emits headerless PCM; this is the rate the WAV header is written with.
DEFAULT_POLLY_SAMPLE_RATE = "16000"
DEFAULT_BLOB_CONTAINER = "podcasts"
# The container is already named `podcasts`; repeating it here produced
# `podcasts/podcasts/<id>/...` in every stored URL (E47).
DEFAULT_BLOB_PREFIX = ""
DEFAULT_BLOB_ACCOUNT = "devstoreaccount1"

UNKNOWN_IMAGE_CONTENT_TYPE = "application/octet-stream"
UNKNOWN_IMAGE_EXTENSION = "bin"

# fal.ai does not guarantee an output format, so the stored name and content type
# are taken from the bytes themselves rather than hardcoded (E46).
_IMAGE_SIGNATURES: tuple[tuple[bytes, str, str], ...] = (
    (b"\xff\xd8\xff", "image/jpeg", "jpg"),
    (b"\x89PNG\r\n\x1a\n", "image/png", "png"),
    (b"GIF87a", "image/gif", "gif"),
    (b"GIF89a", "image/gif", "gif"),
)


def sniff_image_type(data: bytes) -> tuple[str, str]:
    """Return the `(content_type, extension)` the image bytes actually are."""

    for magic, content_type, extension in _IMAGE_SIGNATURES:
        if data.startswith(magic):
            return content_type, extension

    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", "webp"

    return UNKNOWN_IMAGE_CONTENT_TYPE, UNKNOWN_IMAGE_EXTENSION


def account_credentials_from_connection_string(
    connection_string: str | None,
) -> tuple[str | None, str | None]:
    """Pull `(AccountName, AccountKey)` out of an Azure Storage connection string.

    A connection string that authenticates some other way — a bare SAS, or a
    future managed-identity setup — yields a `None` key, which callers treat as
    "cannot sign". The `UseDevelopmentStorage=true` shorthand carries no literal
    values, so the Azure SDK expands it rather than hardcoding emulator secrets.
    """

    if not connection_string:
        return None, None

    values: dict[str, str] = {}
    for segment in connection_string.split(";"):
        key, separator, value = segment.partition("=")
        if separator and value.strip():
            values[key.strip().lower()] = value.strip()

    account_name = values.get("accountname")
    account_key = values.get("accountkey")
    if account_name or account_key:
        return account_name, account_key

    if connection_string.strip().lower() == "usedevelopmentstorage=true":
        return _emulator_credentials()
    return None, None


def _emulator_credentials() -> tuple[str | None, str | None]:
    try:
        from azure.storage.blob import BlobServiceClient  # pyright: ignore[reportMissingImports]

        service_client = BlobServiceClient.from_connection_string("UseDevelopmentStorage=true")
        credential = service_client.credential
        return service_client.account_name, getattr(credential, "account_key", None)
    except Exception:  # pragma: no cover - defensive, the SDK is a hard dependency
        return DEFAULT_BLOB_ACCOUNT, None


def _account_name_from_connection_string(connection_string: str | None) -> str | None:
    """Pull `AccountName` out of an Azure Storage connection string."""

    return account_credentials_from_connection_string(connection_string)[0]


def default_voice_models() -> dict[str, str]:
    return {
        DEFAULT_AUDIO_VOICE: DEFAULT_AUDIO_VOICE_MODEL,
        DEFAULT_HOST_B_VOICE: DEFAULT_HOST_B_VOICE_MODEL,
    }


class PodcastClientError(RuntimeError):
    pass


class PodcastTimeoutError(PodcastClientError):
    pass


class _SupportsParse(Protocol):
    def parse(self, /, **kwargs: Any) -> Any: ...


class _SupportsRun(Protocol):
    def run(self, /, *args: Any, **kwargs: Any) -> Any: ...


class _SupportsPost(Protocol):
    def post(self, /, *args: Any, **kwargs: Any) -> Any: ...


class _SupportsGetBlobClient(Protocol):
    def get_blob_client(self, blob: str) -> Any: ...


_SCRIPT_SYSTEM_PROMPT = (
    "Write a concise two-host podcast script. Use only host_a and host_b. "
    "Keep the output bounded and focused. "
    'Respond with a single JSON object shaped exactly like '
    '{"parts":[{"speaker":"host_a","text":"..."}]} and nothing else. '
    "Do not wrap it in Markdown code fences and do not add commentary."
)


def _strip_code_fence(content: str) -> str:
    """Remove a Markdown fence around a JSON body.

    Nova Pro returns bare JSON, but Nova Lite and Micro wrap it in ```json fences and
    a model swap should not break podcast generation.
    """
    text = content.strip()
    if not text.startswith("```"):
        return text

    without_open = text[3:]
    if without_open.lower().startswith("json"):
        without_open = without_open[4:]
    return without_open.removesuffix("```").strip()


def _pcm_to_wav(pcm: bytes, *, sample_rate: int) -> bytes:
    """Wrap Polly's headerless PCM in a WAV container.

    Polly emits raw signed 16-bit little-endian mono PCM, where slng.ai emitted WAV.
    `merge_audio_clips` reads clips with ``format="wav"``, which pydub handles through
    the stdlib `wave` module -- so adding the header here keeps ffmpeg out of the
    image. Switching the merge step to mp3 instead would drag ffmpeg back in.
    """
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm)
    return buffer.getvalue()


@dataclass(slots=True)
class BedrockScriptGenerator:
    """Podcast script generation on Bedrock Converse.

    Nova is not served on Bedrock's OpenAI-compatible Chat Completions path, so this
    cannot keep using the `openai` SDK. Converse also has no `response_format`, so the
    JSON shape is instructed in the prompt and validated with the same
    ``PodcastScript`` model the SDK used to populate via ``.parse()``.
    """

    client: Any | None = None
    model: str = DEFAULT_BEDROCK_SCRIPT_MODEL
    region: str = "us-east-1"
    timeout_seconds: int = 120
    max_parts: int = 12
    max_tokens: int = DEFAULT_BEDROCK_SCRIPT_MAX_TOKENS

    def __post_init__(self) -> None:
        if self.client is None:
            boto3 = import_module("boto3")
            config = import_module("botocore.config")
            self.client = boto3.client(
                "bedrock-runtime",
                region_name=self.region,
                config=config.Config(
                    read_timeout=self.timeout_seconds,
                    connect_timeout=min(10, self.timeout_seconds),
                ),
            )

    def generate_script(self, *, label: str, chunks: list[str]) -> PodcastScript:
        prompt = self._build_prompt(label=label, chunks=chunks)
        try:
            response = self.client.converse(  # type: ignore[union-attr]
                modelId=self.model,
                system=[{"text": _SCRIPT_SYSTEM_PROMPT}],
                messages=[{"role": "user", "content": [{"text": prompt}]}],
                inferenceConfig={"maxTokens": self.max_tokens},
            )
        except (
            TimeoutError,
            httpx.TimeoutException,
            ReadTimeoutError,
            ConnectTimeoutError,
        ) as exc:
            raise PodcastTimeoutError("Bedrock script generation timed out") from exc
        except Exception as exc:  # pragma: no cover - defensive normalization
            raise PodcastClientError("Bedrock script generation failed") from exc

        # A truncated response is invalid JSON. Reporting the real cause beats a
        # generic parse failure further down.
        if response.get("stopReason") == "max_tokens":
            raise PodcastClientError(
                f"Bedrock script generation hit the {self.max_tokens}-token output limit"
            )

        blocks = response.get("output", {}).get("message", {}).get("content", [])
        content = "".join(block["text"] for block in blocks if "text" in block)
        if not content.strip():
            raise PodcastClientError("Bedrock script response was empty")

        try:
            payload = json.loads(_strip_code_fence(content))
        except json.JSONDecodeError as exc:
            raise PodcastClientError("Bedrock script response was not valid JSON") from exc

        try:
            script = PodcastScript.model_validate(payload)
        except Exception as exc:
            raise PodcastClientError("Bedrock script response did not match the schema") from exc

        # The prompt asks for at most max_parts, but a prompt is not a guarantee.
        if len(script.parts) > self.max_parts:
            script = PodcastScript(parts=script.parts[: self.max_parts])
        return script

    def _build_prompt(self, *, label: str, chunks: list[str]) -> str:
        lines = [f"Topic: {label}", f"Limit script parts to at most {self.max_parts}.", "Chunks:"]
        for index, chunk in enumerate(chunks, start=1):
            lines.append(f"{index}. {chunk}")
        return "\n".join(lines)


@dataclass(slots=True)
class PollyTTSClient:
    """Amazon Polly text-to-speech, returning WAV bytes.

    Returns WAV rather than Polly's native PCM so `merge_audio_clips` and the
    `audio/wav` upload content type stay exactly as they were.
    """

    client: Any | None = None
    region: str = "us-east-1"
    timeout_seconds: int = 30
    engine: str = DEFAULT_POLLY_ENGINE
    sample_rate: str = DEFAULT_POLLY_SAMPLE_RATE
    voice_models: dict[str, str] | None = None

    def __post_init__(self) -> None:
        if self.voice_models is None:
            self.voice_models = default_voice_models()
        if self.client is None:
            boto3 = import_module("boto3")
            config = import_module("botocore.config")
            self.client = boto3.client(
                "polly",
                region_name=self.region,
                config=config.Config(
                    read_timeout=self.timeout_seconds,
                    connect_timeout=min(10, self.timeout_seconds),
                ),
            )

    def synthesize(self, *, text: str, voice: str = DEFAULT_AUDIO_VOICE) -> bytes:
        voice_models = self.voice_models or default_voice_models()
        voice_id = voice_models.get(voice, voice_models[DEFAULT_AUDIO_VOICE])
        try:
            response = self.client.synthesize_speech(  # type: ignore[union-attr]
                Text=text,
                VoiceId=voice_id,
                Engine=self.engine,
                OutputFormat="pcm",
                SampleRate=self.sample_rate,
            )
            pcm = response["AudioStream"].read()
        except (
            TimeoutError,
            httpx.TimeoutException,
            ReadTimeoutError,
            ConnectTimeoutError,
        ) as exc:
            raise PodcastTimeoutError("Polly text-to-speech timed out") from exc
        except Exception as exc:  # pragma: no cover - defensive normalization
            raise PodcastClientError("Polly text-to-speech failed") from exc

        if not pcm:
            raise PodcastClientError("Polly returned no audio")

        return _pcm_to_wav(pcm, sample_rate=int(self.sample_rate))

@dataclass(slots=True)
class FalCoverGenerator:
    client: _SupportsRun | None = None
    model: str = DEFAULT_FAL_MODEL
    api_key: str | None = None
    timeout_seconds: int = 120

    def __post_init__(self) -> None:
        if self.client is None:
            fal_client = __import__("fal_client")
            if self.api_key is None:
                # No configured key: fall back to the module-level client, which
                # reads FAL_KEY from the process environment.
                self.client = fal_client
            else:
                self.client = fal_client.SyncClient(
                    key=self.api_key,
                    default_timeout=self.timeout_seconds,
                )

    def generate_cover(self, *, prompt: str) -> str:
        try:
            result = self.client.run(  # type: ignore[union-attr]
                self.model,
                arguments={"prompt": prompt},
            )
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise PodcastTimeoutError("fal.ai cover generation timed out") from exc
        except Exception as exc:  # pragma: no cover - defensive normalization
            raise PodcastClientError("fal.ai cover generation failed") from exc

        url = self._extract_url(result)
        if url is None:
            raise PodcastClientError("fal.ai cover result did not include a URL")
        return url

    def _extract_url(self, result: Any) -> str | None:
        if isinstance(result, str):
            return result
        if isinstance(result, dict):
            images = result.get("images")
            if isinstance(images, list) and images:
                first_image = images[0]
                if isinstance(first_image, dict):
                    url = first_image.get("url")
                    if isinstance(url, str) and url.strip():
                        return url.strip()
            url = result.get("url")
            if isinstance(url, str) and url.strip():
                return url.strip()
            return None

        images = getattr(result, "images", None)
        if images:
            first_image = images[0]
            url = getattr(first_image, "url", None)
            if isinstance(url, str) and url.strip():
                return url.strip()

        url = getattr(result, "url", None)
        if isinstance(url, str) and url.strip():
            return url.strip()
        return None


@dataclass(slots=True)
class AzurePodcastBlobStore:
    container_client: _SupportsGetBlobClient | None = None
    connection_string: str | None = None
    container_name: str = DEFAULT_BLOB_CONTAINER
    blob_prefix: str = DEFAULT_BLOB_PREFIX
    account_name: str | None = None
    timeout_seconds: int = 120

    def __post_init__(self) -> None:
        if self.account_name is None:
            self.account_name = _account_name_from_connection_string(self.connection_string)
        if self.container_client is None:
            if self.connection_string is None:
                raise ValueError("connection_string is required when no Blob client is injected")
            from azure.storage.blob import BlobServiceClient  # pyright: ignore[reportMissingImports]

            service_client = BlobServiceClient.from_connection_string(self.connection_string)
            self.container_client = service_client.get_container_client(self.container_name)

    def audio_blob_name(self, podcast_id: str) -> str:
        return self._blob_name(podcast_id, "podcast.wav")

    def cover_blob_name(self, podcast_id: str, *, extension: str) -> str:
        return self._blob_name(podcast_id, f"cover.{extension}")

    def _blob_name(self, podcast_id: str, file_name: str) -> str:
        prefix = self.blob_prefix.strip("/")
        if prefix:
            return f"{prefix}/{podcast_id}/{file_name}"
        return f"{podcast_id}/{file_name}"

    def upload_bytes(self, *, blob_name: str, data: bytes, content_type: str) -> str:
        try:
            blob_client = self.container_client.get_blob_client(blob_name)  # type: ignore[union-attr]
            blob_client.upload_blob(
                data,
                overwrite=True,
                content_type=content_type,
                timeout=self.timeout_seconds,
            )
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise PodcastTimeoutError("Azure Blob upload timed out") from exc
        except Exception as exc:  # pragma: no cover - defensive normalization
            raise PodcastClientError("Azure Blob upload failed") from exc

        url = getattr(blob_client, "url", None)
        if isinstance(url, str) and url.strip():
            return url.strip()
        return f"{self._container_base_url()}/{blob_name.lstrip('/')}"

    def upload_audio(self, *, podcast_id: str, audio: bytes) -> str:
        return self.upload_bytes(
            blob_name=self.audio_blob_name(podcast_id),
            data=audio,
            content_type="audio/wav",
        )

    def upload_cover(self, *, podcast_id: str, cover: bytes) -> str:
        content_type, extension = sniff_image_type(cover)
        return self.upload_bytes(
            blob_name=self.cover_blob_name(podcast_id, extension=extension),
            data=cover,
            content_type=content_type,
        )

    def _container_base_url(self) -> str:
        container_url = getattr(self.container_client, "url", None)
        if isinstance(container_url, str) and container_url.strip():
            return container_url.rstrip("/")

        account = self.account_name or DEFAULT_BLOB_ACCOUNT
        return f"https://{account}.blob.core.windows.net/{self.container_name}"


__all__ = [
    "AzurePodcastBlobStore",
    "DEFAULT_AUDIO_VOICE",
    "DEFAULT_AUDIO_VOICE_MODEL",
    "DEFAULT_BLOB_ACCOUNT",
    "DEFAULT_BLOB_CONTAINER",
    "DEFAULT_BLOB_PREFIX",
    "DEFAULT_HOST_B_VOICE",
    "DEFAULT_HOST_B_VOICE_MODEL",
    "DEFAULT_FAL_MODEL",
    "DEFAULT_BEDROCK_SCRIPT_MODEL",
    "DEFAULT_BEDROCK_SCRIPT_MAX_TOKENS",
    "DEFAULT_POLLY_ENGINE",
    "DEFAULT_POLLY_SAMPLE_RATE",
    "FalCoverGenerator",
    "UNKNOWN_IMAGE_CONTENT_TYPE",
    "UNKNOWN_IMAGE_EXTENSION",
    "account_credentials_from_connection_string",
    "BedrockScriptGenerator",
    "PodcastClientError",
    "PodcastTimeoutError",
    "PollyTTSClient",
    "default_voice_models",
    "sniff_image_type",
]
