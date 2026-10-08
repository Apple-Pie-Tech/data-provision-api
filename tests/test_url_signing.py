from __future__ import annotations

from typing import Any

from app.url_signing import (  # pyright: ignore[reportMissingImports]
    DEFAULT_PRESIGNED_URL_TTL_MINUTES,
    S3PresignedUrlSigner,
    split_bucket_and_key,
)


AUDIO_URL = "https://applepie-podcasts.s3.us-east-1.amazonaws.com/podcast-1/podcast.wav"


class FakeS3:
    """Records what the signer asks botocore for, and hands back a stable URL."""

    def __init__(self, *, fail_with: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._fail_with = fail_with

    def generate_presigned_url(self, **kwargs: Any) -> str:
        if self._fail_with is not None:
            raise self._fail_with
        self.calls.append(kwargs)
        params = kwargs["Params"]
        return (
            f"https://{params['Bucket']}.s3.us-east-1.amazonaws.com/{params['Key']}"
            f"?X-Amz-Signature=deadbeef&X-Amz-Expires={kwargs['ExpiresIn']}"
        )


def test_signer_presigns_a_get_on_the_right_bucket_and_key() -> None:
    """E52: a stored S3 URL is not fetchable; the API response must carry a token."""
    fake = FakeS3()
    signer = S3PresignedUrlSigner(client=fake)

    signed = signer.sign(AUDIO_URL)

    assert signed is not None
    assert "X-Amz-Signature=deadbeef" in signed
    assert fake.calls == [
        {
            "ClientMethod": "get_object",
            "Params": {"Bucket": "applepie-podcasts", "Key": "podcast-1/podcast.wav"},
            "ExpiresIn": DEFAULT_PRESIGNED_URL_TTL_MINUTES * 60,
        }
    ]


def test_signer_honours_the_configured_ttl_in_seconds() -> None:
    """ExpiresIn is seconds; passing minutes would make every link 60x shorter."""
    fake = FakeS3()
    signer = S3PresignedUrlSigner(client=fake, ttl_minutes=5)

    signer.sign(AUDIO_URL)

    assert fake.calls[0]["ExpiresIn"] == 300


def test_signer_degrades_to_the_bare_url_when_signing_fails() -> None:
    """No resolvable credentials must not turn every read into a 500.

    botocore resolves credentials lazily, so a local run with no AWS profile
    first finds out inside generate_presigned_url.
    """
    signer = S3PresignedUrlSigner(client=FakeS3(fail_with=RuntimeError("NoCredentials")))

    assert signer.sign(AUDIO_URL) == AUDIO_URL


def test_signer_degrades_when_no_client_can_be_built(monkeypatch) -> None:
    import app.url_signing as url_signing

    monkeypatch.setattr(url_signing, "_shared_s3_client", lambda region: None)
    signer = S3PresignedUrlSigner()

    assert signer.can_sign is False
    assert signer.sign(AUDIO_URL) == AUDIO_URL


def test_signer_passes_through_missing_and_foreign_urls() -> None:
    """A cover that never reached S3 still has to round-trip unchanged."""
    signer = S3PresignedUrlSigner(client=FakeS3())

    assert signer.sign(None) is None
    assert signer.sign("https://cdn.example.com/podcasts/podcast-1/podcast.wav") == (
        "https://cdn.example.com/podcasts/podcast-1/podcast.wav"
    )
    assert signer.sign("data:image/png;base64,iVBORw0KGgo=") == (
        "data:image/png;base64,iVBORw0KGgo="
    )


def test_split_bucket_and_key_handles_every_url_shape_the_stack_writes() -> None:
    assert split_bucket_and_key(AUDIO_URL) == (
        "applepie-podcasts",
        "podcast-1/podcast.wav",
    )
    assert split_bucket_and_key("s3://applepie-audio/audio/input-1/source.wav") == (
        "applepie-audio",
        "audio/input-1/source.wav",
    )
    assert split_bucket_and_key(
        "https://s3.us-east-1.amazonaws.com/applepie-audio/audio/input-1/source.wav"
    ) == ("applepie-audio", "audio/input-1/source.wav")
    assert split_bucket_and_key(
        "https://s3-us-east-1.amazonaws.com/applepie-audio/audio/input-1/source.wav"
    ) == ("applepie-audio", "audio/input-1/source.wav")
    assert split_bucket_and_key(
        "https://applepie-audio.s3.amazonaws.com/audio/input-1/source.wav"
    ) == ("applepie-audio", "audio/input-1/source.wav")


def test_split_bucket_and_key_percent_decodes_the_key() -> None:
    """S3AudioStorage percent-encodes the key into the URL it stores.

    Handing the encoded form to generate_presigned_url would sign a key that
    does not exist, producing a valid-looking URL that 404s.
    """
    assert split_bucket_and_key(
        "https://applepie-audio.s3.us-east-1.amazonaws.com/audio/my%20input/source.wav"
    ) == ("applepie-audio", "audio/my input/source.wav")


def test_split_bucket_and_key_rejects_non_s3_and_incomplete_urls() -> None:
    assert split_bucket_and_key("https://cdn.example.com/podcasts/p/podcast.wav") is None
    assert split_bucket_and_key("https://applepie-podcasts.s3.us-east-1.amazonaws.com/") is None
    assert split_bucket_and_key("https://s3.us-east-1.amazonaws.com/applepie-audio") is None
    assert split_bucket_and_key("s3://applepie-audio") is None
    assert split_bucket_and_key("not a url at all") is None
