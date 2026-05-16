from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from io import BytesIO
from typing import Any, Protocol

import httpx

from app.audio import merge_audio_clips  # pyright: ignore[reportMissingImports]
from app.podcast_clients import (  # pyright: ignore[reportMissingImports]
    AzurePodcastBlobStore,
    DEFAULT_AUDIO_VOICE,
    FalCoverGenerator,
    OpenAIScriptGenerator,
    PodcastClientError,
    PodcastTimeoutError,
    SlngTTSClient,
)
from app.podcast_repository import PodcastRepository  # pyright: ignore[reportMissingImports]
from app.podcast_schemas import PodcastDetail, PodcastScript
from app.vector_store import VectorPoint  # pyright: ignore[reportMissingImports]


FALLBACK_COVER_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\x0cIDATx\x9cc\xf8\xff\xff?\x00"
    b"\x05\xfe\x02\xfe\xdc\xccY\xe7\x00\x00\x00\x00IEND\xaeB`\x82"
)


class SupportsCoverDownload(Protocol):
    def get(self, url: str, **kwargs: Any) -> Any: ...


class SupportsPodcastRepository(Protocol):
    def mark_running(self, podcast_id: str) -> PodcastDetail: ...

    def mark_completed(
        self,
        podcast_id: str,
        *,
        script: PodcastScript,
        audio_url: str | None,
        cover_url: str | None,
    ) -> PodcastDetail: ...

    def mark_failed(self, podcast_id: str, *, error: str) -> PodcastDetail: ...

    def get_by_id(self, podcast_id: str) -> PodcastDetail | None: ...


class SupportsPointReader(Protocol):
    async def read_points_for_label(self, label: str | None) -> list[VectorPoint]: ...


class SupportsScriptGenerator(Protocol):
    def generate_script(self, *, label: str, chunks: list[str]) -> PodcastScript: ...


class SupportsTTSClient(Protocol):
    def synthesize(self, *, text: str, voice: str) -> bytes: ...


class SupportsBlobStore(Protocol):
    def upload_audio(self, *, podcast_id: str, audio: bytes) -> str: ...

    def upload_cover(self, *, podcast_id: str, cover: bytes) -> str: ...


class SupportsCoverGenerator(Protocol):
    def generate_cover(self, *, prompt: str) -> str: ...


AudioMerger = Callable[[Sequence[bytes]], bytes]


@dataclass(slots=True)
class PodcastGenerationResult:
    podcast: PodcastDetail
    cover_used_fallback: bool = False


def merge_audio_clips(clips: Sequence[bytes]) -> bytes:
    if not clips:
        raise ValueError("no audio clips were generated")

    from pydub import AudioSegment  # pyright: ignore[reportMissingImports]

    merged = AudioSegment.empty()
    for clip in clips:
        if not clip:
            raise ValueError("audio clip was empty")
        merged += AudioSegment.from_file(BytesIO(clip), format="mp3")

    output = BytesIO()
    merged.export(output, format="mp3")
    return output.getvalue()


async def generate_podcast(
    podcast_id: str,
    *,
    repository: SupportsPodcastRepository,
    point_reader: SupportsPointReader,
    script_generator: SupportsScriptGenerator,
    tts_client: SupportsTTSClient,
    blob_store: SupportsBlobStore,
    cover_generator: SupportsCoverGenerator | None = None,
    cover_downloader: SupportsCoverDownload | None = None,
    audio_merger: AudioMerger | None = None,
    max_chunks: int,
    max_script_parts: int,
    timeout_seconds: int,
) -> PodcastGenerationResult:
    running = repository.mark_running(podcast_id)

    if audio_merger is None:
        audio_merger = merge_audio_clips

    try:
        points = await point_reader.read_points_for_label(running.label)
        chunks = extract_chunks(points, max_chunks=max_chunks)
        if not chunks:
            raise ValueError(f"no chunks found for label '{running.label}'")

        script = bound_script(
            script_generator.generate_script(label=running.label, chunks=chunks),
            max_parts=max_script_parts,
        )
        if not script.parts:
            raise ValueError(f"generated script was empty for label '{running.label}'")

        audio_clips = synthesize_script_audio(script, tts_client=tts_client)
        merged_audio = audio_merger(audio_clips)
        if not merged_audio:
            raise ValueError("merged podcast audio was empty")

        audio_url = blob_store.upload_audio(podcast_id=podcast_id, audio=merged_audio)
        cover_url, used_fallback = generate_cover_asset(
            podcast_id,
            label=running.label,
            blob_store=blob_store,
            cover_generator=cover_generator,
            cover_downloader=cover_downloader,
            timeout_seconds=timeout_seconds,
        )

        completed = repository.mark_completed(
            podcast_id,
            script=script,
            audio_url=audio_url,
            cover_url=cover_url,
        )
        return PodcastGenerationResult(podcast=completed, cover_used_fallback=used_fallback)
    except Exception as exc:
        failed = mark_generation_failed(repository, podcast_id, exc)
        return PodcastGenerationResult(podcast=failed, cover_used_fallback=False)


def extract_chunks(points: Sequence[VectorPoint], *, max_chunks: int) -> list[str]:
    chunks: list[str] = []
    for point in points:
        text = optional_text(point.text)
        if text is None:
            continue
        chunks.append(text)
        if len(chunks) >= max_chunks:
            break
    return chunks


def bound_script(script: PodcastScript, *, max_parts: int) -> PodcastScript:
    if len(script.parts) <= max_parts:
        return script
    return PodcastScript(parts=script.parts[:max_parts])


def synthesize_script_audio(script: PodcastScript, *, tts_client: SlngTTSClient) -> list[bytes]:
    audio_clips: list[bytes] = []

    for part in script.parts:
        text = optional_text(part.text)
        if text is None:
            continue
        voice = optional_text(part.speaker) or DEFAULT_AUDIO_VOICE
        audio_clips.append(tts_client.synthesize(text=text, voice=voice))

    if not audio_clips:
        raise ValueError("generated script did not contain speakable parts")
    return audio_clips


def generate_cover_asset(
    podcast_id: str,
    *,
    label: str,
    blob_store: SupportsBlobStore,
    cover_generator: SupportsCoverGenerator | None,
    cover_downloader: SupportsCoverDownload | None,
    timeout_seconds: int,
) -> tuple[str | None, bool]:
    if cover_generator is None:
        return upload_fallback_cover(blob_store, podcast_id=podcast_id)

    try:
        cover_url = cover_generator.generate_cover(prompt=build_cover_prompt(label))
        cover_bytes = download_cover_bytes(
            cover_url,
            cover_downloader=cover_downloader,
            timeout_seconds=timeout_seconds,
        )
        return blob_store.upload_cover(podcast_id=podcast_id, cover=cover_bytes), False
    except Exception:
        return upload_fallback_cover(blob_store, podcast_id=podcast_id)


def upload_fallback_cover(blob_store: SupportsBlobStore, *, podcast_id: str) -> tuple[str | None, bool]:
    try:
        return blob_store.upload_cover(podcast_id=podcast_id, cover=FALLBACK_COVER_PNG), True
    except Exception:
        return None, True


def download_cover_bytes(
    url: str,
    *,
    cover_downloader: SupportsCoverDownload | None,
    timeout_seconds: int,
) -> bytes:
    if cover_downloader is not None:
        response = cover_downloader.get(url, timeout=timeout_seconds)
        response.raise_for_status()
        return bytes(response.content)

    with httpx.Client(timeout=timeout_seconds) as client:
        response = client.get(url)
        response.raise_for_status()
        return bytes(response.content)


def build_cover_prompt(label: str) -> str:
    return f"Podcast cover art for {label}, simple editorial illustration, no text"


def mark_generation_failed(
    repository: SupportsPodcastRepository,
    podcast_id: str,
    exc: Exception,
) -> PodcastDetail:
    error = normalize_generation_error(exc)
    try:
        current = repository.get_by_id(podcast_id)
        if current is not None and current.status == "running":
            return repository.mark_failed(podcast_id, error=error)
        if current is not None:
            return current
    except Exception:
        pass

    raise exc


def normalize_generation_error(exc: Exception) -> str:
    if isinstance(exc, (PodcastClientError, PodcastTimeoutError, ValueError)):
        message = str(exc).strip() or "podcast generation failed"
    else:
        message = "podcast generation failed"
    return message[:300]


def optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


__all__ = [
    "FALLBACK_COVER_PNG",
    "PodcastGenerationResult",
    "build_cover_prompt",
    "bound_script",
    "download_cover_bytes",
    "extract_chunks",
    "generate_cover_asset",
    "generate_podcast",
    "mark_generation_failed",
    "merge_audio_clips",
    "normalize_generation_error",
    "synthesize_script_audio",
]
