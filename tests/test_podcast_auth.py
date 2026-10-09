"""Auth contract for POST /podcasts (x-api-key).

Mirrors data-ingestion's /ingest check. The header name is part of the contract
with the UI (applepie-ui/src/features/universe/provision-client.ts) and must not
drift.

POST /podcasts is gated because it is the endpoint that spends money: one
Bedrock call plus several Polly calls plus S3 writes per request. The read
endpoints are deliberately not gated, matching data-ingestion, where the same
check guards POST /ingest and nothing else -- there are tests below pinning
both halves of that decision so neither changes silently.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import (
    Settings,
    app,
    get_podcast_generation_dependencies,
    get_podcast_repository,
    get_point_reader,
    get_settings,
)
from app.podcast_schemas import PodcastDetail

API_KEY = "test-provision-key"


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


@pytest.fixture
def repository(monkeypatch: pytest.MonkeyPatch) -> FakeRepository:
    fake = FakeRepository()
    app.dependency_overrides[get_podcast_repository] = lambda: fake
    app.dependency_overrides[get_point_reader] = lambda: FakePointReader()
    app.dependency_overrides[get_podcast_generation_dependencies] = lambda: None

    # With no injected dependencies the route schedules generation as a
    # background task, and TestClient runs those before returning. Stub it out:
    # these tests are about the auth gate, and the real task would build boto3
    # clients and then call Bedrock and Polly.
    scheduled: list[str] = []

    async def fake_generation(podcast_id: str, settings: Settings) -> None:
        scheduled.append(podcast_id)

    monkeypatch.setattr("app.main.run_podcast_generation_from_settings", fake_generation)
    fake.scheduled = scheduled  # type: ignore[attr-defined]

    yield fake
    app.dependency_overrides.clear()


def _with_key(key: str | None) -> None:
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None, provision_api_key=key
    )


def test_create_podcast_rejects_a_missing_key(repository: FakeRepository) -> None:
    _with_key(API_KEY)
    client = TestClient(app)

    response = client.post("/podcasts", json={"label": "product-updates"})

    assert response.status_code == 401
    assert response.json() == {"detail": "unauthorized"}
    assert repository.created == [], "the row must not be created"
    assert repository.scheduled == [], "generation must not be scheduled"


def test_create_podcast_rejects_a_wrong_key(repository: FakeRepository) -> None:
    _with_key(API_KEY)
    client = TestClient(app)

    response = client.post(
        "/podcasts",
        json={"label": "product-updates"},
        headers={"x-api-key": "not-the-key"},
    )

    assert response.status_code == 401
    assert repository.created == []


def test_the_401_does_not_leak_the_expected_key(repository: FakeRepository) -> None:
    _with_key(API_KEY)
    client = TestClient(app)

    response = client.post(
        "/podcasts", json={"label": "x"}, headers={"x-api-key": "wrong"}
    )

    assert API_KEY not in response.text
    assert response.json() == {"detail": "unauthorized"}


def test_create_podcast_accepts_the_right_key(repository: FakeRepository) -> None:
    _with_key(API_KEY)
    client = TestClient(app)

    response = client.post(
        "/podcasts",
        json={"label": "product-updates"},
        headers={"x-api-key": API_KEY},
    )

    assert response.status_code == 202
    assert repository.created == ["product-updates"]
    # And the expensive part was actually reached, so a 202 here means the gate
    # let the request through rather than the route failing earlier.
    assert repository.scheduled == ["podcast-1"]


def test_the_header_name_is_x_api_key(repository: FakeRepository) -> None:
    """The UI sends `x-api-key`; a rename here breaks it silently."""
    _with_key(API_KEY)
    client = TestClient(app)

    wrong_header = client.post(
        "/podcasts", json={"label": "x"}, headers={"api-key": API_KEY}
    )
    right_header = client.post(
        "/podcasts", json={"label": "x"}, headers={"x-api-key": API_KEY}
    )

    assert wrong_header.status_code == 401
    assert right_header.status_code == 202


@pytest.mark.parametrize("key", [None, "", "   "])
def test_an_unset_key_skips_the_check(key: str | None, repository: FakeRepository) -> None:
    """Failing closed would stop a fresh clone from generating anything.

    create_app logs a warning instead, so an unprotected deployment is not
    silent.
    """
    _with_key(key)
    client = TestClient(app)

    response = client.post("/podcasts", json={"label": "product-updates"})

    assert response.status_code == 202
    assert repository.created == ["product-updates"]


def test_create_app_warns_when_the_key_is_unset(caplog) -> None:
    from app.main import create_app

    with caplog.at_level("WARNING"):
        create_app(Settings(_env_file=None, provision_api_key=None))

    assert "PROVISION_API_KEY is not set" in caplog.text


def test_create_app_does_not_warn_when_the_key_is_set(caplog) -> None:
    from app.main import create_app

    with caplog.at_level("WARNING"):
        create_app(Settings(_env_file=None, provision_api_key=API_KEY))

    assert "PROVISION_API_KEY is not set" not in caplog.text


def test_the_read_endpoints_stay_open(repository: FakeRepository) -> None:
    """Deliberate, and pinned so it is a decision rather than an oversight.

    Gating reads would need the UI to send the key on every request; the money
    is spent by the write path. infrastructure/aws/README.md records that these
    remain public.
    """
    _with_key(API_KEY)
    client = TestClient(app)

    assert client.get("/universe").status_code == 200
    assert client.get("/podcasts").status_code == 200
    assert client.get("/health").status_code == 200
