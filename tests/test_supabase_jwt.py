"""Verification of the Supabase access token, exercised without a network.

Every test mints its own token with an ephemeral P-256 keypair generated in
process, so the suite needs neither a live Supabase project nor a committed key
fixture. The JWKS fetch is injected, so nothing here opens a socket.

The tests that matter most are the ones pinning what the verifier must
*refuse*: a token signed by a different key under the same key id, an unsigned
token, and an HS256 token where no symmetric secret is configured. Those are
the algorithm-confusion family, and a verifier that merely "decodes the token"
passes all the happy-path tests while failing every one of these.
"""

from __future__ import annotations

import json
import logging
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from app.supabase_jwt import (
    SupabaseJwtError,
    SupabaseJwtUnavailableError,
    SupabaseJwtVerifier,
    issuer_for,
    jwks_url_for,
)

SUPABASE_URL = "https://project.supabase.co"
ISSUER = f"{SUPABASE_URL}/auth/v1"
AUDIENCE = "authenticated"
USER_ID = "11111111-2222-3333-4444-555555555555"
# Real wall-clock time, deliberately. The verifier's injected `now` drives only
# the JWKS cache TTL; PyJWT validates `exp` and `iat` against the real clock, so
# a pinned constant here would make every token expired and every acceptance
# test fail -- which is exactly what a fixed epoch did on the first run.
NOW = time.time()


def _keypair(kid: str = "test-kid"):
    """An ephemeral signing key plus the JWKS document that publishes it."""
    private_key = ec.generate_private_key(ec.SECP256R1())
    jwk = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(private_key.public_key()))
    jwk |= {"kid": kid, "alg": "ES256", "use": "sig"}
    return private_key, {"keys": [jwk]}


def _token(private_key, *, kid: str = "test-kid", algorithm: str = "ES256", **overrides) -> str:
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": USER_ID,
        "role": "authenticated",
        "email": "someone@example.com",
        "iat": int(NOW) - 10,
        "exp": int(NOW) + 3600,
    }
    claims.update(overrides)
    for key, value in list(claims.items()):
        if value is None:
            del claims[key]
    return jwt.encode(claims, private_key, algorithm=algorithm, headers={"kid": kid})


class _FakeResponse:
    def __init__(self, document) -> None:
        self._document = document

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._document


class FakeJwksClient:
    """Stands in for httpx.AsyncClient, counting fetches."""

    def __init__(self, document, *, error: Exception | None = None) -> None:
        self.document = document
        self.error = error
        self.calls = 0
        self.urls: list[str] = []

    async def get(self, url: str):
        self.calls += 1
        self.urls.append(url)
        if self.error is not None:
            raise self.error
        return _FakeResponse(self.document)


def _verifier(client, *, now=None, **kwargs) -> SupabaseJwtVerifier:
    clock = now if now is not None else (lambda: NOW)
    return SupabaseJwtVerifier(
        supabase_url=SUPABASE_URL,
        audience=AUDIENCE,
        http_client=client,
        now=clock,
        **kwargs,
    )


def test_issuer_and_jwks_url_tolerate_a_trailing_slash() -> None:
    assert issuer_for("https://project.supabase.co/") == ISSUER
    assert jwks_url_for("https://project.supabase.co/") == (
        f"{ISSUER}/.well-known/jwks.json"
    )


@pytest.mark.asyncio
async def test_a_token_signed_by_the_published_key_verifies() -> None:
    private_key, document = _keypair()
    verifier = _verifier(FakeJwksClient(document))

    identity = await verifier.verify(_token(private_key))

    assert identity.user_id == USER_ID
    assert identity.email == "someone@example.com"
    assert identity.claims["role"] == "authenticated"


@pytest.mark.asyncio
async def test_a_token_signed_by_a_different_key_with_the_same_kid_is_rejected() -> None:
    """The signature check itself.

    Both keypairs publish `kid` "test-kid", so a verifier that looked the key up
    by id and skipped the cryptography would accept this. That is the whole
    point of the test.
    """
    _, document = _keypair()
    impostor_key, _ = _keypair()
    verifier = _verifier(FakeJwksClient(document))

    with pytest.raises(SupabaseJwtError):
        await verifier.verify(_token(impostor_key))


@pytest.mark.asyncio
async def test_an_expired_token_is_rejected() -> None:
    private_key, document = _keypair()
    verifier = _verifier(FakeJwksClient(document))

    with pytest.raises(SupabaseJwtError):
        await verifier.verify(_token(private_key, exp=int(NOW) - 1, iat=int(NOW) - 3600))


@pytest.mark.asyncio
async def test_an_unsigned_token_is_rejected() -> None:
    """`alg: none` is the other half of the algorithm-confusion family."""
    _, document = _keypair()
    verifier = _verifier(FakeJwksClient(document))
    unsigned = jwt.encode(
        {"iss": ISSUER, "aud": AUDIENCE, "sub": USER_ID, "iat": int(NOW), "exp": int(NOW) + 60},
        key="",
        algorithm="none",
    )

    with pytest.raises(SupabaseJwtError):
        await verifier.verify(unsigned)


@pytest.mark.asyncio
async def test_an_hs256_token_is_rejected_without_fetching_keys_when_no_secret_is_set() -> None:
    """An HS256 token in the asymmetric regime is an attack, not an outage.

    It must raise SupabaseJwtError (401), never SupabaseJwtUnavailableError
    (503), and it must not cost a JWKS fetch -- otherwise it is a free way to
    make this service hammer Supabase.
    """
    _, document = _keypair()
    client = FakeJwksClient(document)
    verifier = _verifier(client)
    forged = jwt.encode(
        {"iss": ISSUER, "aud": AUDIENCE, "sub": USER_ID, "iat": int(NOW), "exp": int(NOW) + 60},
        key="not-the-secret",
        algorithm="HS256",
    )

    with pytest.raises(SupabaseJwtError):
        await verifier.verify(forged)

    assert client.calls == 0


@pytest.mark.asyncio
async def test_a_token_from_another_issuer_is_rejected() -> None:
    private_key, document = _keypair()
    verifier = _verifier(FakeJwksClient(document))

    with pytest.raises(SupabaseJwtError):
        await verifier.verify(_token(private_key, iss="https://evil.supabase.co/auth/v1"))


@pytest.mark.asyncio
async def test_a_token_for_another_audience_is_rejected() -> None:
    private_key, document = _keypair()
    verifier = _verifier(FakeJwksClient(document))

    with pytest.raises(SupabaseJwtError):
        await verifier.verify(_token(private_key, aud="anon"))


@pytest.mark.asyncio
async def test_a_token_without_a_subject_is_rejected() -> None:
    private_key, document = _keypair()
    verifier = _verifier(FakeJwksClient(document))

    with pytest.raises(SupabaseJwtError):
        await verifier.verify(_token(private_key, sub=None))


@pytest.mark.asyncio
async def test_a_token_with_no_key_id_is_rejected() -> None:
    private_key, document = _keypair()
    verifier = _verifier(FakeJwksClient(document))
    token = jwt.encode(
        {"iss": ISSUER, "aud": AUDIENCE, "sub": USER_ID, "iat": int(NOW), "exp": int(NOW) + 60},
        private_key,
        algorithm="ES256",
    )

    with pytest.raises(SupabaseJwtError):
        await verifier.verify(token)


@pytest.mark.asyncio
async def test_a_blank_token_is_rejected() -> None:
    _, document = _keypair()
    verifier = _verifier(FakeJwksClient(document))

    with pytest.raises(SupabaseJwtError):
        await verifier.verify("   ")


@pytest.mark.asyncio
async def test_the_keys_are_fetched_once_and_cached() -> None:
    private_key, document = _keypair()
    client = FakeJwksClient(document)
    verifier = _verifier(client)

    await verifier.verify(_token(private_key))
    await verifier.verify(_token(private_key))

    assert client.calls == 1
    assert client.urls[0] == f"{ISSUER}/.well-known/jwks.json"


@pytest.mark.asyncio
async def test_the_cache_expires_on_wall_clock_time() -> None:
    """Measured with time.time(), so a frozen-then-thawed Lambda sees the gap."""
    private_key, document = _keypair()
    client = FakeJwksClient(document)
    clock = {"now": NOW}
    verifier = _verifier(client, now=lambda: clock["now"], cache_seconds=600)

    await verifier.verify(_token(private_key))
    clock["now"] = NOW + 601
    await verifier.verify(_token(private_key))

    assert client.calls == 2


@pytest.mark.asyncio
async def test_an_unknown_key_id_refetches_once_the_cooldown_has_passed() -> None:
    """What a key rotation looks like from here, and the flood guard on it.

    The cooldown is measured from the last successful fetch, so an unknown key
    id arriving moments after one does NOT trigger another: the document is
    already current, and re-fetching would make a stream of bogus key ids into
    a stream of upstream requests. The cost is that a genuinely new signing key
    is unknown for at most `min_refetch_seconds`, which a retry resolves.
    """
    private_key, document = _keypair(kid="published")
    client = FakeJwksClient(document)
    clock = {"now": NOW}
    verifier = _verifier(client, now=lambda: clock["now"], min_refetch_seconds=30)

    await verifier.verify(_token(private_key, kid="published"))
    assert client.calls == 1

    # Freshly fetched, so an unknown kid is answered from what we already have.
    with pytest.raises(SupabaseJwtError):
        await verifier.verify(_token(private_key, kid="rotated"))
    assert client.calls == 1

    # Past the cooldown, the same unknown kid is worth one re-fetch.
    clock["now"] = NOW + 31
    with pytest.raises(SupabaseJwtError):
        await verifier.verify(_token(private_key, kid="rotated"))
    assert client.calls == 2

    # And immediately after that re-fetch, another unknown kid is free again.
    with pytest.raises(SupabaseJwtError):
        await verifier.verify(_token(private_key, kid="rotated-again"))
    assert client.calls == 2


@pytest.mark.asyncio
async def test_a_key_added_after_the_cache_was_filled_is_picked_up() -> None:
    """The rotation actually completing: a new kid resolves after a re-fetch."""
    first_key, document = _keypair(kid="old")
    client = FakeJwksClient(document)
    clock = {"now": NOW}
    verifier = _verifier(client, now=lambda: clock["now"], min_refetch_seconds=30)

    await verifier.verify(_token(first_key, kid="old"))

    rotated_key, rotated_document = _keypair(kid="new")
    client.document = {"keys": document["keys"] + rotated_document["keys"]}
    clock["now"] = NOW + 31

    identity = await verifier.verify(_token(rotated_key, kid="new"))

    assert identity.user_id == USER_ID
    assert client.calls == 2


@pytest.mark.asyncio
async def test_an_unreachable_jwks_is_unavailable_and_never_invalid(caplog) -> None:
    """503, not 401 and certainly not 200.

    A 401 would send every signed-in user to the sign-in screen during a
    Supabase blip; a 200 would be an authentication bypass triggered by a
    network error.
    """
    private_key, _ = _keypair()
    client = FakeJwksClient(None, error=RuntimeError("connection refused"))
    verifier = _verifier(client)

    with caplog.at_level(logging.ERROR, logger="app.supabase_jwt"):
        with pytest.raises(SupabaseJwtUnavailableError):
            await verifier.verify(_token(private_key))

    records = [r for r in caplog.records if r.name == "app.supabase_jwt"]
    assert records, "the cause was swallowed"
    assert records[0].exc_info is not None


@pytest.mark.asyncio
async def test_a_stale_cache_survives_a_failed_refresh() -> None:
    """Availability during an outage, without extending trust past the TTL."""
    private_key, document = _keypair()
    client = FakeJwksClient(document)
    clock = {"now": NOW}
    verifier = _verifier(client, now=lambda: clock["now"], cache_seconds=600)

    await verifier.verify(_token(private_key))

    client.error = RuntimeError("supabase is down")
    clock["now"] = NOW + 601
    identity = await verifier.verify(_token(private_key))

    assert identity.user_id == USER_ID
    assert client.calls == 2


@pytest.mark.asyncio
async def test_the_legacy_hs256_regime_verifies_without_touching_the_jwks() -> None:
    client = FakeJwksClient({"keys": []})
    verifier = _verifier(client, hs256_secret="a-local-development-secret")
    token = jwt.encode(
        {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": USER_ID,
            "iat": int(NOW) - 10,
            "exp": int(NOW) + 3600,
        },
        "a-local-development-secret",
        algorithm="HS256",
    )

    identity = await verifier.verify(token)

    assert identity.user_id == USER_ID
    assert client.calls == 0


@pytest.mark.asyncio
async def test_an_hs256_token_signed_with_the_wrong_secret_is_rejected() -> None:
    client = FakeJwksClient({"keys": []})
    verifier = _verifier(client, hs256_secret="the-real-secret")
    token = jwt.encode(
        {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": USER_ID,
            "iat": int(NOW) - 10,
            "exp": int(NOW) + 3600,
        },
        "a-guess",
        algorithm="HS256",
    )

    with pytest.raises(SupabaseJwtError):
        await verifier.verify(token)


@pytest.mark.asyncio
async def test_an_unusable_jwks_entry_is_skipped_rather_than_fatal() -> None:
    private_key, document = _keypair(kid="good")
    document["keys"].append({"kid": "broken", "kty": "OKP", "crv": "Ed448", "x": "nonsense"})
    verifier = _verifier(FakeJwksClient(document))

    identity = await verifier.verify(_token(private_key, kid="good"))

    assert identity.user_id == USER_ID


@pytest.mark.asyncio
async def test_the_injected_client_is_the_only_transport_used(monkeypatch) -> None:
    """Belt and braces: proves the suite cannot silently start using the network."""
    import httpx

    def explode(*args, **kwargs):
        raise AssertionError("the verifier built its own HTTP client")

    monkeypatch.setattr(httpx, "AsyncClient", explode)

    private_key, document = _keypair()
    verifier = _verifier(FakeJwksClient(document))

    identity = await verifier.verify(_token(private_key))

    assert identity.user_id == USER_ID
