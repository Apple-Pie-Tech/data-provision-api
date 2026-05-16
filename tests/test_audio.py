from __future__ import annotations

import sys
import types
from io import BytesIO

import pytest

from app.audio import merge_audio_clips  # pyright: ignore[reportMissingImports]


def test_merge_audio_clips_rejects_empty_input() -> None:
    with pytest.raises(ValueError, match="no audio clips were generated"):
        merge_audio_clips([])


def test_merge_audio_clips_rejects_empty_clip() -> None:
    _install_fake_pydub()
    with pytest.raises(ValueError, match="audio clip was empty"):
        merge_audio_clips([b"", b"clip"])


class FakeAudioSegment:
    def __init__(self, payload: bytes = b"") -> None:
        self.payloads = [payload] if payload else []

    @classmethod
    def empty(cls) -> "FakeAudioSegment":
        return cls()

    @classmethod
    def from_file(cls, fileobj: BytesIO, format: str) -> "FakeAudioSegment":
        assert format == "mp3"
        return cls(fileobj.read())

    def __iadd__(self, other: "FakeAudioSegment") -> "FakeAudioSegment":
        self.payloads.extend(other.payloads)
        return self

    def export(self, output: BytesIO, format: str) -> BytesIO:
        assert format == "mp3"
        output.write(b"merged:" + b"|".join(self.payloads))
        return output


def _install_fake_pydub() -> None:
    module = types.ModuleType("pydub")
    setattr(module, "AudioSegment", FakeAudioSegment)
    sys.modules["pydub"] = module


def test_merge_audio_clips_combines_mp3_segments() -> None:
    _install_fake_pydub()
    clip_a = b"clip-a"
    clip_b = b"clip-b"

    merged = merge_audio_clips([clip_a, clip_b])

    assert isinstance(merged, bytes)
    assert merged == b"merged:clip-a|clip-b"
