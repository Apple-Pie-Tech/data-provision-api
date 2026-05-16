"""Audio helpers for podcast generation.

Requires `ffmpeg` to be installed and available on `PATH` so pydub can decode
and export MP3 files.
"""

from __future__ import annotations

from collections.abc import Sequence
from io import BytesIO


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


__all__ = ["merge_audio_clips"]
