"""Verify the Supabase access token the UI already holds.

This is what makes the write endpoints *authenticated* rather than merely
deterred. The `x-api-key` check beside it is documented as a deterrent and
cannot be more than that: the UI holds its key in an `EXPO_PUBLIC_*` variable,
which is inlined into the public JavaScript bundle, so every visitor has it.
A Supabase access token, by contrast, is signed by the Supabase project and
names the user in its `sub` claim, which is something a caller cannot forge.

Three design points worth stating, because each one is a trap:

* **Nothing here is imported at module scope.** `import jwt` pulls in
  `cryptography`, which is the single largest import in either service.
  data-ingestion already logs `INIT_REPORT ... Status: timeout` at Lambda's 10s
  init limit, so a new top-level import would make a measured problem worse.
  The import happens inside `verify()`, on the first authenticated request --
  the same trick `app/secrets.py` already uses for boto3.

* **The JWKS document is fetched lazily and cached on the instance**, never at
  import. Under the Lambda Web Adapter the uvicorn process survives between
  invocations, so instance state *is* the cross-invocation cache; no file in
  /tmp and no table are needed. The TTL is measured with `time.time()` rather
  than `time.monotonic()` on purpose: Lambda *freezes* a sandbox instead of
  killing it, and a frozen-then-thawed process must see the real elapsed wall
  time or it would keep a rotated-out key well past its TTL.

* **"Cannot check the token" is not "token is invalid".** An unreachable JWKS
  endpoint raises `SupabaseJwtUnavailableError`, which the caller maps to 503.
  Answering 401 would send every signed-in user to the sign-in screen during a
  Supabase outage that is not their fault, and answering 200 would be an
  authentication bypass triggered by a network blip.

This module is duplicated verbatim in data-ingestion and data-provision-api.
That is deliberate and follows the precedent of `app/secrets.py`, which is
already near-identical in both: they are separate repositories with no shared
package, and publishing one for ~200 lines would be a larger change than the
duplication it removes.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from importlib import import_module
from typing import Any, Protocol

logger = logging.getLogger(__name__)

# How long a fetched JWKS document is trusted before it is re-fetched.
DEFAULT_JWKS_CACHE_SECONDS = 600
# Floor between forced re-fetches, so a flood of tokens carrying unknown `kid`
# values cannot be turned into one upstream request per request.
DEFAULT_JWKS_MIN_REFETCH_SECONDS = 30
DEFAULT_JWKS_TIMEOUT_SECONDS = 2.0

# Asymmetric algorithms, verified against the project's published public keys.
# A fixed whitelist is what makes it safe to read the algorithm out of the
# token's own header: each branch binds a key *type* to the algorithm before
# decoding, so an HS256 token can only ever be checked against the shared
# secret and an ES256/RS256 token only ever against a JWKS public key. Without
# that binding, a caller could sign a token with the *public* key as an HMAC
# secret and have it accepted -- the classic algorithm-confusion attack.
ASYMMETRIC_ALGORITHMS = ("ES256", "RS256")
SYMMETRIC_ALGORITHMS = ("HS256",)


class SupabaseJwtError(Exception):
    """The token is not acceptable. Maps to 401.

    Never carries the token or any part of it: the message is logged and the
    response body is a fixed string.
    """


class SupabaseJwtUnavailableError(Exception):
    """The token could not be *checked*. Maps to 503, never to 401 or 200."""


class SupportsJwksFetch(Protocol):
    """The slice of an httpx.AsyncClient this module uses."""

    async def get(self, url: str) -> Any: ...


@dataclass(frozen=True, slots=True)
class SupabaseIdentity:
    """A verified caller. `user_id` is the token's `sub` claim."""

    user_id: str
    email: str | None
    claims: dict[str, Any]


def issuer_for(supabase_url: str) -> str:
    """The `iss` a Supabase project puts in its tokens."""

    return f"{supabase_url.rstrip('/')}/auth/v1"


def jwks_url_for(supabase_url: str) -> str:
    return f"{issuer_for(supabase_url)}/.well-known/jwks.json"


class SupabaseJwtVerifier:
    """Verifies Supabase access tokens, caching the project's public keys.

    Supports both of the regimes a Supabase project can be in. Which one
    applies is decided by the project, not by this code: a populated `keys`
    array at the JWKS endpoint means asymmetric signing (ES256/RS256) and no
    shared secret exists anywhere; an empty one means the project is still on
    the legacy HS256 shared secret, which must then be supplied as
    `hs256_secret`. Asymmetric is strictly preferable -- the HS256 secret both
    verifies *and* mints tokens, so anything that can read it can forge any
    user's identity.
    """

    def __init__(
        self,
        *,
        supabase_url: str,
        audience: str | None = "authenticated",
        hs256_secret: str | None = None,
        cache_seconds: int = DEFAULT_JWKS_CACHE_SECONDS,
        min_refetch_seconds: int = DEFAULT_JWKS_MIN_REFETCH_SECONDS,
        timeout_seconds: float = DEFAULT_JWKS_TIMEOUT_SECONDS,
        http_client: SupportsJwksFetch | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._issuer = issuer_for(supabase_url)
        self._jwks_url = jwks_url_for(supabase_url)
        self._audience = audience
        self._hs256_secret = (hs256_secret or "").strip() or None
        self._cache_seconds = cache_seconds
        self._min_refetch_seconds = min_refetch_seconds
        self._timeout_seconds = timeout_seconds
        self._http_client = http_client
        self._now = now

        self._keys: dict[str, Any] = {}
        self._fetched_at: float | None = None
        self._lock = asyncio.Lock()

    @property
    def issuer(self) -> str:
        return self._issuer

    async def verify(self, token: str) -> SupabaseIdentity:
        """Return the verified identity, or raise.

        Raises `SupabaseJwtError` for anything wrong with the token and
        `SupabaseJwtUnavailableError` when the keys could not be obtained.
        """
        jwt = _load_pyjwt()

        if not token or not token.strip():
            raise SupabaseJwtError("no token supplied")

        try:
            header = jwt.get_unverified_header(token)
        except Exception as exc:
            raise SupabaseJwtError("token header is not readable") from exc

        algorithm = header.get("alg")
        key = await self._key_for(algorithm, header.get("kid"))

        try:
            claims = jwt.decode(
                token,
                key=key,
                algorithms=[algorithm],
                issuer=self._issuer,
                audience=self._audience,
                options={
                    "require": ["exp", "iat", "iss", "sub"],
                    "verify_aud": self._audience is not None,
                },
            )
        except Exception as exc:
            # Covers expiry, a bad signature, a wrong issuer or audience and a
            # missing required claim. All of them are the caller's problem, and
            # the distinction is deliberately not reported back to them.
            raise SupabaseJwtError(f"token rejected: {type(exc).__name__}") from exc

        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject:
            raise SupabaseJwtError("token has no usable subject")

        email = claims.get("email")
        return SupabaseIdentity(
            user_id=subject,
            email=email if isinstance(email, str) and email else None,
            claims=claims,
        )

    async def _key_for(self, algorithm: Any, kid: Any) -> Any:
        """Resolve the verification key, binding key type to algorithm."""

        if algorithm in SYMMETRIC_ALGORITHMS:
            if self._hs256_secret is None:
                # Not an outage: an HS256 token arriving at a service configured
                # for asymmetric verification is an algorithm-confusion attempt,
                # so it is rejected rather than reported as unavailable.
                raise SupabaseJwtError(
                    "token is HS256 but no symmetric secret is configured"
                )
            return self._hs256_secret

        if algorithm not in ASYMMETRIC_ALGORITHMS:
            # Includes "none", which is the other half of the classic attack.
            raise SupabaseJwtError(f"unsupported algorithm {algorithm!r}")

        if not isinstance(kid, str) or not kid:
            raise SupabaseJwtError("asymmetric token carries no key id")

        key = await self._signing_key(kid)
        if key is None:
            raise SupabaseJwtError("no published key matches the token's key id")
        return key

    async def _signing_key(self, kid: str) -> Any:
        await self._ensure_keys()
        key = self._keys.get(kid)
        if key is not None:
            return key

        # The key id is unknown, which is what a key rotation looks like from
        # here. Re-fetch once, rate-limited, before giving up.
        await self._ensure_keys(force=True)
        return self._keys.get(kid)

    async def _ensure_keys(self, *, force: bool = False) -> None:
        async with self._lock:
            now = self._now()
            fetched_at = self._fetched_at

            if fetched_at is not None:
                age = now - fetched_at
                if force:
                    if age < self._min_refetch_seconds:
                        return
                elif age < self._cache_seconds:
                    return

            try:
                document = await self._fetch_jwks()
            except Exception as exc:
                if self._keys:
                    # A stale document is better than an outage: it was valid
                    # when fetched and the keys in it have not been withdrawn.
                    # Trust is not extended -- the next request tries again.
                    logger.warning(
                        "could not refresh the Supabase JWKS, using the cached "
                        "copy: %s",
                        exc,
                    )
                    return
                logger.exception("could not fetch the Supabase JWKS")
                raise SupabaseJwtUnavailableError(
                    "the Supabase signing keys are unreachable"
                ) from exc

            self._keys = _parse_jwks(document)
            self._fetched_at = now

    async def _fetch_jwks(self) -> Any:
        if self._http_client is not None:
            response = await self._http_client.get(self._jwks_url)
            response.raise_for_status()
            return response.json()

        httpx = import_module("httpx")
        async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
            response = await client.get(self._jwks_url)
            response.raise_for_status()
            return response.json()

    def reset_cache(self) -> None:
        """Drop the cached keys. For tests; nothing in the app calls this."""

        self._keys = {}
        self._fetched_at = None


def _load_pyjwt() -> Any:
    """Import PyJWT on first use, keeping `cryptography` off the init path."""

    try:
        return import_module("jwt")
    except Exception as exc:  # pragma: no cover - a packaging failure
        raise SupabaseJwtUnavailableError("PyJWT is not installed") from exc


def _parse_jwks(document: Any) -> dict[str, Any]:
    """Turn a JWKS document into `{kid: key}`, skipping anything unusable.

    An unreadable individual key is skipped rather than fatal: a project may
    publish a key type this service does not support alongside one it does, and
    rejecting the whole document would take authentication down with it.
    """
    jwt = _load_pyjwt()

    keys: dict[str, Any] = {}
    entries = document.get("keys") if isinstance(document, dict) else None
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        kid = entry.get("kid")
        if not isinstance(kid, str) or not kid:
            continue
        try:
            keys[kid] = jwt.PyJWK.from_dict(entry).key
        except Exception as exc:  # noqa: BLE001 - one bad key is not fatal
            logger.warning("skipping an unusable JWKS entry %s: %s", kid, exc)

    if not keys:
        # Reached when the project is still on legacy HS256 signing, which
        # publishes an empty `keys` array. Verification of an asymmetric token
        # will fail with "no published key matches", which is accurate.
        logger.warning("the Supabase JWKS document published no usable keys")

    return keys


__all__ = [
    "ASYMMETRIC_ALGORITHMS",
    "DEFAULT_JWKS_CACHE_SECONDS",
    "DEFAULT_JWKS_MIN_REFETCH_SECONDS",
    "SupabaseIdentity",
    "SupabaseJwtError",
    "SupabaseJwtUnavailableError",
    "SupabaseJwtVerifier",
    "issuer_for",
    "jwks_url_for",
]
