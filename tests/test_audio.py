from __future__ import annotations

import sys
import types
import wave
from io import BytesIO

import pytest

from app.audio import merge_audio_clips  # pyright: ignore[reportMissingImports]


def test_merge_audio_clips_rejects_empty_input() -> None:
    with pytest.raises(ValueError, match="no audio clips were generated"):
        merge_audio_clips([])


def test_merge_audio_clips_rejects_empty_clip(fake_pydub: None) -> None:
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
        assert format == "wav"
        return cls(fileobj.read())

    def __iadd__(self, other: "FakeAudioSegment") -> "FakeAudioSegment":
        self.payloads.extend(other.payloads)
        return self

    def export(self, output: BytesIO, format: str) -> BytesIO:
        assert format == "wav"
        output.write(b"merged:" + b"|".join(self.payloads))
        return output


@pytest.fixture
def fake_pydub(monkeypatch: pytest.MonkeyPatch) -> None:
    """Swap in a fake `pydub` for one test only, then put the real one back.

    `monkeypatch.setitem` restores the previous entry (or removes the key if
    there was none) at teardown, so the fake never leaks into other tests.
    """

    module = types.ModuleType("pydub")
    setattr(module, "AudioSegment", FakeAudioSegment)
    monkeypatch.setitem(sys.modules, "pydub", module)


def test_merge_audio_clips_combines_wav_segments(fake_pydub: None) -> None:
    clip_a = b"clip-a"
    clip_b = b"clip-b"

    merged = merge_audio_clips([clip_a, clip_b])

    assert isinstance(merged, bytes)
    assert merged == b"merged:clip-a|clip-b"


def _wav_clip(*, frames: int) -> bytes:
    buffer = BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(24000)
        handle.writeframes(b"\x00\x00" * frames)
    return buffer.getvalue()


def test_merge_audio_clips_concatenates_real_wav_clips() -> None:
    # The real pydub is an on-disk module; the fake one has no __file__.
    loaded_pydub = sys.modules.get("pydub")
    assert loaded_pydub is None or hasattr(loaded_pydub, "__file__"), (
        "the fake pydub leaked out of its fixture"
    )

    merged = merge_audio_clips([_wav_clip(frames=240), _wav_clip(frames=360)])

    with wave.open(BytesIO(merged), "rb") as handle:
        assert handle.getnchannels() == 1
        assert handle.getsampwidth() == 2
        assert handle.getframerate() == 24000
        assert handle.getnframes() == 600
