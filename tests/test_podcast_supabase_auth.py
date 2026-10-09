"""Auth contract for POST /podcasts' Supabase bearer token.

Separate from test_podcast_auth.py, which pins the `x-api-key` deterrent; this
file pins real authentication. Both gates run during the transition and their
401s are deliberately identical from outside.

The assertions that matter most are not the status codes but
`repository.created == []` and `repository.scheduled == []`: POST /podcasts
spends a Bedrock call, several Polly calls and S3 writes, so a refused request
must not create a row or schedule generation. A gate that returns 401 *after*
starting the work would pass a status-code-only test.

Tokens are minted with an ephemeral keypair and the JWKS fetch is injected, so
nothing here needs the network or a live Supabase project.
"""

from __future__ import annotations

import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from app.main import (
    Settings,
    app,
    get_podcast_generation_dependencies,
    get_podcast_repository,
    get_point_reader,
    get_settings,
    get_supabase_verifier,
)
from app.podcast_schemas import PodcastDetail
from app.supabase_jwt import SupabaseJwtVerifier

SUPABASE_URL = "https://project.supabase.co"
ISSUER = f"{SUPABASE_URL}/auth/v1"
USER_ID = "11111111-2222-3333-4444-555555555555"
KID = "provision-test-kid"


class FakeRepository:
    def __init__(self) -> None:
        self.created: list[str] = []

    def init_db(self) -> None:
        return None

    def close(self) -> None:
        return None

    def create(self, label: str) -> PodcastDetail:
        self.created.append(label)
        return PodcastDetail(id="podcast-1", label=label, status="pending")

    def list(self) -> list[PodcastDetail]:
        return []

    def get_by_id(self, podcast_id: str) -> PodcastDetail | None:
        return None


class FakePointReader:
    async def read_points(self) -> list[object]:
        return []


class _FakeResponse:
    def __init__(self, document) -> None:
        self._document = document

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._document


class _FakeJwksClient:
    def __init__(self, document, *, error: Exception | None = None) -> None:
        self._document = document
        self._error = error

    async def get(self, url: str):
        if self._error is not None:
            raise self._error
        return _FakeResponse(self._document)


_PRIVATE_KEY = ec.generate_private_key(ec.SECP256R1())
_JWK = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(_PRIVATE_KEY.public_key()))
_JWK |= {"kid": KID, "alg": "ES256", "use": "sig"}
_JWKS = {"keys": [_JWK]}


def _token(*, key=None, **overrides) -> str:
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": "authenticated",
        "sub": USER_ID,
        "role": "authenticated",
        "iat": now - 10,
        "exp": now + 3600,
    }
    claims.update(overrides)
    return jwt.encode(claims, key or _PRIVATE_KEY, algorithm="ES256", headers={"kid": KID})


def _bearer(token: str | None = None) -> dict[str, str]:
    return {"Authorization": f"Bearer {token or _token()}"}


@pytest.fixture
def repository(monkeypatch: pytest.MonkeyPatch) -> FakeRepository:
    fake = FakeRepository()
    app.dependency_overrides[get_podcast_repository] = lambda: fake
    app.dependency_overrides[get_point_reader] = lambda: FakePointReader()
    app.dependency_overrides[get_podcast_generation_dependencies] = lambda: None

    scheduled: list[str] = []

    async def fake_generation(podcast_id: str, settings: Settings) -> None:
        scheduled.append(podcast_id)

    monkeypatch.setattr("app.main.run_podcast_generation_from_settings", fake_generation)
    fake.scheduled = scheduled  # type: ignore[attr-defined]

    yield fake
    app.dependency_overrides.clear()


def _configured(*, error: Exception | None = None) -> None:
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None, supabase_url=SUPABASE_URL
    )
    app.dependency_overrides[get_supabase_verifier] = lambda: SupabaseJwtVerifier(
        supabase_url=SUPABASE_URL,
        audience="authenticated",
        http_client=_FakeJwksClient(_JWKS, error=error),
    )


def _unconfigured() -> None:
    app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None, supabase_url=None)


def test_podcasts_stay_open_when_supabase_url_is_unset(repository: FakeRepository) -> None:
    """Unset means skip, the same convention as PROVISION_API_KEY.

    This is what lets the services deploy with verification off and be switched
    on later by a Terraform apply, instead of needing all three repositories to
    deploy at the same instant.
    """
    _unconfigured()
    client = TestClient(app)

    response = client.post("/podcasts", json={"label": "product-updates"})

    assert response.status_code == 202
    assert repository.created == ["product-updates"]


def test_a_valid_bearer_token_is_accepted(repository: FakeRepository) -> None:
    _configured()
    client = TestClient(app)

    response = client.post("/podcasts", json={"label": "product-updates"}, headers=_bearer())

    assert response.status_code == 202
    assert repository.created == ["product-updates"]


def test_a_missing_authorization_header_spends_nothing(repository: FakeRepository) -> None:
    _configured()
    client = TestClient(app)

    response = client.post("/podcasts", json={"label": "product-updates"})

    assert response.status_code == 401
    assert response.json() == {"detail": "unauthorized"}
    assert repository.created == [], "the row must not be created"
    assert repository.scheduled == [], "generation must not be scheduled"


def test_a_token_signed_by_another_key_spends_nothing(repository: FakeRepository) -> None:
    _configured()
    impostor = ec.generate_private_key(ec.SECP256R1())
    client = TestClient(app)

    response = client.post(
        "/podcasts",
        json={"label": "product-updates"},
        headers=_bearer(_token(key=impostor)),
    )

    assert response.status_code == 401
    assert repository.created == []
    assert repository.scheduled == []


def test_an_expired_token_spends_nothing(repository: FakeRepository) -> None:
    _configured()
    now = int(time.time())
    client = TestClient(app)

    response = client.post(
        "/podcasts",
        json={"label": "product-updates"},
        headers=_bearer(_token(exp=now - 1, iat=now - 3600)),
    )

    assert response.status_code == 401
    assert repository.created == []
    assert repository.scheduled == []


def test_a_token_for_another_issuer_spends_nothing(repository: FakeRepository) -> None:
    _configured()
    client = TestClient(app)

    response = client.post(
        "/podcasts",
        json={"label": "product-updates"},
        headers=_bearer(_token(iss="https://evil.supabase.co/auth/v1")),
    )

    assert response.status_code == 401
    assert repository.created == []


def test_a_non_bearer_scheme_is_rejected(repository: FakeRepository) -> None:
    _configured()
    client = TestClient(app)

    response = client.post(
        "/podcasts",
        json={"label": "product-updates"},
        headers={"Authorization": "Basic dXNlcjpwYXNz"},
    )

    assert response.status_code == 401
    assert repository.created == []


def test_an_unreachable_jwks_is_a_503_not_a_401(repository: FakeRepository) -> None:
    """A Supabase outage is not the caller's session being invalid."""
    _configured(error=RuntimeError("connection refused"))
    client = TestClient(app)

    response = client.post("/podcasts", json={"label": "product-updates"}, headers=_bearer())

    assert response.status_code == 503
    assert repository.created == [], "nothing may be created when auth is undecidable"


def test_the_read_endpoints_stay_open_under_supabase_auth(repository: FakeRepository) -> None:
    """Re-pins the open-reads decision under the new regime.

    test_podcast_auth.py pins this for the API key; without the same test here,
    turning on SUPABASE_URL could silently close the reads and no test would
    notice. Still a deliberate decision, still recorded in
    infrastructure/aws/README.md.
    """
    _configured()
    client = TestClient(app)

    assert client.get("/universe").status_code == 200
    assert client.get("/podcasts").status_code == 200
    assert client.get("/health").status_code == 200
