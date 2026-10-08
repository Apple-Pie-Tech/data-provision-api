"""Audio helpers for podcast generation.

PollyTTSClient wraps Polly's headerless PCM into WAV, so clips are decoded
and re-exported as WAV. pydub reads and writes WAV through the standard library,
so `ffmpeg` is only needed if another codec is ever introduced.
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
        merged += AudioSegment.from_file(BytesIO(clip), format="wav")

    output = BytesIO()
    merged.export(output, format="wav")
    return output.getvalue()


__all__ = ["merge_audio_clips"]
