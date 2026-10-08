"""Read-time SAS signing for stored blob URLs (E52).

Storage keeps the canonical, unsigned blob URL — Postgres for podcasts, the
Qdrant payload for ingested audio. A token is minted only while a response is
being built, so a persisted record can never rot into a broken link.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import unquote, urlsplit, urlunsplit

from app.podcast_clients import (  # pyright: ignore[reportMissingImports]
    account_credentials_from_connection_string,
)
from app.podcast_schemas import PodcastListItem
from app.schemas import UniverseResponse


DEFAULT_BLOB_SAS_TTL_MINUTES = 60


def split_container_and_blob(url: str, *, account_name: str) -> tuple[str, str] | None:
    """Split a blob URL into `(container, blob)` for the given storage account.

    Returns `None` when the URL belongs to another account or is not a blob
    URL at all — callers then hand the URL back untouched rather than raising.
    """

    parts = urlsplit(url)
    host = parts.hostname or ""
    segments = [segment for segment in parts.path.split("/") if segment]

    if host.split(".")[0] == account_name:
        # `https://<account>.blob.core.windows.net/<container>/<blob>`
        pass
    elif segments and segments[0] == account_name:
        # Emulator/path style: `http://host:port/<account>/<container>/<blob>`
        segments = segments[1:]
    else:
        return None

    if len(segments) < 2:
        return None
    return segments[0], unquote("/".join(segments[1:]))


@dataclass(slots=True)
class BlobSasUrlSigner:
    """Appends a short-lived read-only SAS token to blob URLs."""

    connection_string: str | None = None
    ttl_minutes: int = DEFAULT_BLOB_SAS_TTL_MINUTES
    account_name: str | None = None
    account_key: str | None = None

    def __post_init__(self) -> None:
        if self.account_name is None or self.account_key is None:
            name, key = account_credentials_from_connection_string(self.connection_string)
            self.account_name = self.account_name or name
            self.account_key = self.account_key or key

    @property
    def can_sign(self) -> bool:
        return bool(self.account_name and self.account_key)

    def sign(self, url: str | None) -> str | None:
        if not url or not self.can_sign:
            return url

        account_name = self.account_name or ""
        location = split_container_and_blob(url, account_name=account_name)
        if location is None:
            return url
        container_name, blob_name = location

        from azure.storage.blob import (  # pyright: ignore[reportMissingImports]
            BlobSasPermissions,
            generate_blob_sas,
        )

        token = generate_blob_sas(
            account_name=account_name,
            container_name=container_name,
            blob_name=blob_name,
            account_key=self.account_key,
            permission=BlobSasPermissions(read=True),
            expiry=datetime.now(UTC) + timedelta(minutes=self.ttl_minutes),
        )
        parts = urlsplit(url)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, token, ""))


def sign_podcast_urls[ItemT: PodcastListItem](podcast: ItemT, signer: BlobSasUrlSigner) -> ItemT:
    return podcast.model_copy(
        update={
            "audio_url": signer.sign(podcast.audio_url),
            "cover_url": signer.sign(podcast.cover_url),
        }
    )


def sign_universe_audio_urls(
    universe: UniverseResponse,
    signer: BlobSasUrlSigner,
) -> UniverseResponse:
    return universe.model_copy(
        update={
            "points": [
                point.model_copy(update={"audio_url": signer.sign(point.audio_url)})
                for point in universe.points
            ]
        }
    )


__all__ = [
    "DEFAULT_BLOB_SAS_TTL_MINUTES",
    "BlobSasUrlSigner",
    "sign_podcast_urls",
    "sign_universe_audio_urls",
    "split_container_and_blob",
]
