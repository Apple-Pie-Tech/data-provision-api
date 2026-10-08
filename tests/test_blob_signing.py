from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

from app.blob_signing import (  # pyright: ignore[reportMissingImports]
    DEFAULT_BLOB_SAS_TTL_MINUTES,
    BlobSasUrlSigner,
    split_container_and_blob,
)


# Not a credential: a syntactically valid base64 string so the signer can run its HMAC.
TEST_ACCOUNT_KEY = base64.b64encode(b"data-provision-api-unit-test").decode()
TEST_CONNECTION_STRING = (
    "DefaultEndpointsProtocol=https;AccountName=applepiestories;"
    f"AccountKey={TEST_ACCOUNT_KEY};EndpointSuffix=core.windows.net"
)
AUDIO_URL = "https://applepiestories.blob.core.windows.net/podcasts/podcast-1/podcast.wav"


def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query)


def test_signer_mints_a_read_only_time_limited_token() -> None:
    """E52: a bare blob URL is not fetchable; the API response must carry a SAS."""

    signer = BlobSasUrlSigner(connection_string=TEST_CONNECTION_STRING)

    signed = signer.sign(AUDIO_URL)

    assert signed is not None
    assert signed.startswith(f"{AUDIO_URL}?")
    query = _query(signed)
    assert query["sp"] == ["r"]
    assert query["sig"]
    expiry = datetime.fromisoformat(query["se"][0].replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    assert now < expiry <= now + timedelta(minutes=DEFAULT_BLOB_SAS_TTL_MINUTES + 1)


def test_signer_honours_the_configured_ttl() -> None:
    signer = BlobSasUrlSigner(connection_string=TEST_CONNECTION_STRING, ttl_minutes=5)

    signed = signer.sign(AUDIO_URL)

    assert signed is not None
    expiry = datetime.fromisoformat(_query(signed)["se"][0].replace("Z", "+00:00"))
    assert expiry <= datetime.now(timezone.utc) + timedelta(minutes=6)


def test_signer_degrades_to_the_bare_url_without_an_account_key() -> None:
    """A keyless connection string (e.g. managed identity) must not break reads."""

    signer = BlobSasUrlSigner(
        connection_string=(
            "BlobEndpoint=https://applepiestories.blob.core.windows.net;"
            "SharedAccessSignature=placeholder"
        )
    )

    assert signer.can_sign is False
    assert signer.sign(AUDIO_URL) == AUDIO_URL


def test_signer_passes_through_missing_and_foreign_urls() -> None:
    signer = BlobSasUrlSigner(connection_string=TEST_CONNECTION_STRING)

    assert signer.sign(None) is None
    assert signer.sign("https://cdn.example.com/podcasts/podcast-1/podcast.wav") == (
        "https://cdn.example.com/podcasts/podcast-1/podcast.wav"
    )


def test_signer_reads_emulator_credentials_from_the_development_shorthand() -> None:
    signer = BlobSasUrlSigner(connection_string="UseDevelopmentStorage=true")

    assert signer.account_name == "devstoreaccount1"
    assert signer.can_sign is True

    signed = signer.sign(
        "http://127.0.0.1:10000/devstoreaccount1/ingest-audio/audio/input-1/source.wav"
    )

    assert signed is not None
    assert _query(signed)["sp"] == ["r"]


def test_split_container_and_blob_handles_host_and_path_style_accounts() -> None:
    assert split_container_and_blob(AUDIO_URL, account_name="applepiestories") == (
        "podcasts",
        "podcast-1/podcast.wav",
    )
    assert split_container_and_blob(
        "http://127.0.0.1:10000/devstoreaccount1/ingest-audio/audio/input-1/source.wav",
        account_name="devstoreaccount1",
    ) == ("ingest-audio", "audio/input-1/source.wav")
    assert split_container_and_blob(
        "https://someoneelse.blob.core.windows.net/podcasts/p/podcast.wav",
        account_name="applepiestories",
    ) is None
    assert split_container_and_blob(
        "https://applepiestories.blob.core.windows.net/podcasts",
        account_name="applepiestories",
    ) is None
