"""Read-time presigning for stored S3 object URLs (E52).

Storage keeps the canonical, unsigned object URL — the podcasts table for
podcasts, the vector payload for ingested audio. A token is minted only while a
response is being built, so a persisted record can never rot into a broken link.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from importlib import import_module
from typing import Any
from urllib.parse import unquote, urlsplit

from app.podcast_schemas import PodcastListItem
from app.schemas import UniverseResponse


DEFAULT_PRESIGNED_URL_TTL_MINUTES = 60


def split_bucket_and_key(url: str) -> tuple[str, str] | None:
    """Split an S3 URL into `(bucket, key)`.

    Handles the three forms this stack can hold:

    * `s3://bucket/key`
    * virtual-hosted `https://bucket.s3.<region>.amazonaws.com/key` — what
      `S3AudioStorage` and `S3PodcastBlobStore` write
    * path style `https://s3.<region>.amazonaws.com/bucket/key`

    Returns `None` for anything else — a third-party cover URL, a local path, a
    hostname that is not S3 — and callers then hand the URL back untouched
    rather than raising. A bucket name containing a literal `s3` label would be
    mis-split, which is why the deployed bucket names have no dots in them.
    """

    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    segments = [segment for segment in parts.path.split("/") if segment]

    if scheme == "s3":
        if not parts.netloc or not segments:
            return None
        return parts.netloc, unquote("/".join(segments))

    if not host.endswith(".amazonaws.com"):
        return None

    labels = host.split(".")

    if "s3" in labels and labels.index("s3") > 0:
        bucket = ".".join(labels[: labels.index("s3")])
        if not bucket or not segments:
            return None
        return bucket, unquote("/".join(segments))

    # Path style, including the legacy `s3-<region>` spelling.
    if labels and labels[0].startswith("s3"):
        if len(segments) < 2:
            return None
        return segments[0], unquote("/".join(segments[1:]))

    return None


@lru_cache(maxsize=4)
def _shared_s3_client(region: str) -> Any | None:
    """One S3 client per region, built on first use.

    The signer is a per-request FastAPI dependency, and building a botocore
    client is not free, so the client is cached here rather than per instance.
    Returns `None` when boto3 cannot produce one at all, which keeps the
    no-credentials path a degradation rather than a 500.
    """

    try:
        boto3 = import_module("boto3")
        return boto3.client("s3", region_name=region)
    except Exception:
        return None


@dataclass(slots=True)
class S3PresignedUrlSigner:
    """Replaces a stored unsigned S3 URL with a short-lived presigned one."""

    region: str = "us-east-1"
    ttl_minutes: int = DEFAULT_PRESIGNED_URL_TTL_MINUTES
    client: Any | None = field(default=None)

    @property
    def can_sign(self) -> bool:
        return self._client() is not None

    def sign(self, url: str | None) -> str | None:
        if not url:
            return url

        client = self._client()
        if client is None:
            return url

        location = split_bucket_and_key(url)
        if location is None:
            return url
        bucket, key = location

        try:
            return client.generate_presigned_url(
                ClientMethod="get_object",
                Params={"Bucket": bucket, "Key": key},
                ExpiresIn=self.ttl_minutes * 60,
            )
        except Exception:
            # Unsigned beats a 500. Credentials are resolved lazily by botocore,
            # so a local run with no AWS profile first finds out here; the Azure
            # signer degraded the same way when the connection string carried no
            # account key.
            return url

    def _client(self) -> Any | None:
        if self.client is not None:
            return self.client
        return _shared_s3_client(self.region)


def sign_podcast_urls[ItemT: PodcastListItem](
    podcast: ItemT,
    signer: S3PresignedUrlSigner,
) -> ItemT:
    return podcast.model_copy(
        update={
            "audio_url": signer.sign(podcast.audio_url),
            "cover_url": signer.sign(podcast.cover_url),
        }
    )


def sign_universe_audio_urls(
    universe: UniverseResponse,
    signer: S3PresignedUrlSigner,
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
    "DEFAULT_PRESIGNED_URL_TTL_MINUTES",
    "S3PresignedUrlSigner",
    "sign_podcast_urls",
    "sign_universe_audio_urls",
    "split_bucket_and_key",
]
