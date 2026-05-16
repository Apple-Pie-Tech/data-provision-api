from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from openai import OpenAI  # pyright: ignore[reportMissingImports]

from app.podcast_schemas import PodcastScript


DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
DEFAULT_SLANG_TTS_PATH = "https://api.slng.ai/v1/tts/slng/deepgram/aura:2"
DEFAULT_FAL_MODEL = "fal-ai/flux/schnell"
DEFAULT_AUDIO_VOICE = "host_a"
DEFAULT_COVER_VOICE = "host_b"
DEFAULT_BLOB_CONTAINER = "podcasts"
DEFAULT_BLOB_PREFIX = "podcasts"


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


@dataclass(slots=True)
class OpenAIScriptGenerator:
    client: Any | None = None
    model: str = DEFAULT_OPENAI_MODEL
    timeout_seconds: int = 120
    max_parts: int = 12

    def __post_init__(self) -> None:
        if self.client is None:
            self.client = OpenAI(timeout=self.timeout_seconds)

    def generate_script(self, *, label: str, chunks: list[str]) -> PodcastScript:
        prompt = self._build_prompt(label=label, chunks=chunks)
        try:
            completion = self.client.chat.completions.parse(  # type: ignore[union-attr]
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Write a concise two-host podcast script. Use only host_a and "
                            "host_b. Keep the output bounded and focused."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                response_format=PodcastScript,
                timeout=self.timeout_seconds,
            )
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise PodcastTimeoutError("OpenAI script generation timed out") from exc
        except Exception as exc:  # pragma: no cover - defensive normalization
            raise PodcastClientError("OpenAI script generation failed") from exc

        parsed = getattr(completion.choices[0].message, "parsed", None)
        if parsed is None:
            raise PodcastClientError("OpenAI script response was not parsed")

        script = parsed if isinstance(parsed, PodcastScript) else PodcastScript.model_validate(parsed)
        if len(script.parts) > self.max_parts:
            script = PodcastScript(parts=script.parts[: self.max_parts])
        return script

    def _build_prompt(self, *, label: str, chunks: list[str]) -> str:
        lines = [f"Topic: {label}", f"Limit script parts to at most {self.max_parts}.", "Chunks:"]
        for index, chunk in enumerate(chunks, start=1):
            lines.append(f"{index}. {chunk}")
        return "\n".join(lines)


@dataclass(slots=True)
class SlngTTSClient:
    client: _SupportsPost | None = None
    api_key: str | None = None
    timeout_seconds: int = 30
    endpoint: str = DEFAULT_SLANG_TTS_PATH
    voice_models: dict[str, str] | None = None

    def __post_init__(self) -> None:
        if self.voice_models is None:
            self.voice_models = {
                DEFAULT_AUDIO_VOICE: "aura-2-theia-en",
                DEFAULT_COVER_VOICE: "aura-2-asteria-en",
            }
        if self.client is None:
            if self.api_key is None:
                raise ValueError("api_key is required when no HTTP client is injected")
            self.client = httpx.Client(
                timeout=httpx.Timeout(self.timeout_seconds),
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            )

    def synthesize(self, *, text: str, voice: str = DEFAULT_AUDIO_VOICE) -> bytes:
        voice_models = self.voice_models or {
            DEFAULT_AUDIO_VOICE: "aura-2-theia-en",
            DEFAULT_COVER_VOICE: "aura-2-asteria-en",
        }
        payload = {"text": text, "model": voice_models.get(voice, voice_models[DEFAULT_AUDIO_VOICE])}
        try:
            response = self.client.post(self.endpoint, json=payload, timeout=self.timeout_seconds)  # type: ignore[union-attr]
            response.raise_for_status()
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise PodcastTimeoutError("slng.ai text-to-speech timed out") from exc
        except Exception as exc:  # pragma: no cover - defensive normalization
            raise PodcastClientError("slng.ai text-to-speech failed") from exc
        return bytes(response.content)


@dataclass(slots=True)
class FalCoverGenerator:
    client: _SupportsRun | None = None
    model: str = DEFAULT_FAL_MODEL
    timeout_seconds: int = 120

    def __post_init__(self) -> None:
        if self.client is None:
            self.client = __import__("fal_client")

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
    timeout_seconds: int = 120

    def __post_init__(self) -> None:
        if self.container_client is None:
            if self.connection_string is None:
                raise ValueError("connection_string is required when no Blob client is injected")
            from azure.storage.blob import BlobServiceClient  # pyright: ignore[reportMissingImports]

            service_client = BlobServiceClient.from_connection_string(self.connection_string)
            self.container_client = service_client.get_container_client(self.container_name)

    @staticmethod
    def audio_blob_name(podcast_id: str) -> str:
        return f"{DEFAULT_BLOB_PREFIX}/{podcast_id}/podcast.mp3"

    @staticmethod
    def cover_blob_name(podcast_id: str) -> str:
        return f"{DEFAULT_BLOB_PREFIX}/{podcast_id}/cover.png"

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
            content_type="audio/mpeg",
        )

    def upload_cover(self, *, podcast_id: str, cover: bytes) -> str:
        return self.upload_bytes(
            blob_name=self.cover_blob_name(podcast_id),
            data=cover,
            content_type="image/png",
        )

    def _container_base_url(self) -> str:
        account_url = getattr(self.container_client, "url", None)
        if isinstance(account_url, str) and account_url.strip():
            return account_url.rstrip("/")
        return f"https://{self.container_name}.blob.core.windows.net"


__all__ = [
    "AzurePodcastBlobStore",
    "DEFAULT_AUDIO_VOICE",
    "DEFAULT_BLOB_CONTAINER",
    "DEFAULT_BLOB_PREFIX",
    "DEFAULT_COVER_VOICE",
    "DEFAULT_FAL_MODEL",
    "DEFAULT_OPENAI_MODEL",
    "DEFAULT_SLANG_TTS_PATH",
    "FalCoverGenerator",
    "OpenAIScriptGenerator",
    "PodcastClientError",
    "PodcastTimeoutError",
    "SlngTTSClient",
]
