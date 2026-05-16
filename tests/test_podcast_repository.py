from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any

import pytest

from app.db import CREATE_PODCASTS_TABLE_SQL, SupportsDatabaseConnection, init_db  # pyright: ignore[reportMissingImports]
from app.podcast_repository import PodcastRepository  # pyright: ignore[reportMissingImports]
from app.podcast_schemas import PodcastListItem, PodcastScript


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class FakeCursor:
    def __init__(self, connection: "FakeConnection") -> None:
        self.connection = connection
        self._rows: list[dict[str, Any]] = []
        self.last_query: str | None = None
        self.last_params: tuple[Any, ...] | None = None

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def execute(self, query: str, params: tuple[Any, ...] | None = None) -> None:
        self.last_query = query.strip()
        self.last_params = params or ()
        self.connection.last_query = self.last_query
        normalized = " ".join(self.last_query.split()).lower()

        if normalized.startswith("create table if not exists podcasts"):
            self.connection.table_created = True
            self._rows = []
            return

        if normalized.startswith("insert into podcasts"):
            podcast_id, label, status = self.last_params
            row = {
                "id": podcast_id,
                "label": label,
                "status": status,
                "script_json": None,
                "audio_url": None,
                "cover_url": None,
                "error": None,
                "created_at": _utc_now(),
                "updated_at": _utc_now(),
                "started_at": None,
                "completed_at": None,
            }
            self.connection.rows[podcast_id] = row
            self._rows = [row]
            return

        if normalized.startswith("select status from podcasts"):
            podcast_id = self.last_params[0]
            row = self.connection.rows.get(podcast_id)
            self._rows = [] if row is None else [{"status": row["status"]}]
            return

        if normalized.startswith("select id, label, status, script_json, audio_url, cover_url, error"):
            podcast_id = self.last_params[0]
            row = self.connection.rows.get(podcast_id)
            self._rows = [] if row is None else [row]
            return

        if normalized.startswith("update podcasts"):
            status = self.last_params[0]
            if status == "running":
                _, podcast_id = self.last_params
                row = self.connection.rows[podcast_id]
                row["status"] = status
                row["started_at"] = _utc_now()
                row["updated_at"] = _utc_now()
                self._rows = [row]
                return

            if status == "completed":
                _, script_json, audio_url, cover_url, podcast_id = self.last_params
                row = self.connection.rows[podcast_id]
                row["status"] = status
                row["script_json"] = json.loads(script_json)
                row["audio_url"] = audio_url
                row["cover_url"] = cover_url
                row["error"] = None
                row["completed_at"] = _utc_now()
                row["updated_at"] = _utc_now()
                self._rows = [row]
                return

            if status == "failed":
                _, error, podcast_id = self.last_params
                row = self.connection.rows[podcast_id]
                row["status"] = status
                row["error"] = error
                row["completed_at"] = _utc_now()
                row["updated_at"] = _utc_now()
                self._rows = [row]
                return

        if normalized.startswith("select id, label, status, audio_url, cover_url from podcasts"):
            rows = list(self.connection.rows.values())
            self._rows = rows
            return

        raise AssertionError(f"Unexpected query: {query}")

    def fetchone(self) -> dict[str, Any] | None:
        if not self._rows:
            return None
        return self._rows[0]

    def fetchall(self) -> list[dict[str, Any]]:
        return list(self._rows)


class FakeConnection(SupportsDatabaseConnection):
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.table_created = False
        self.commits = 0
        self.rollbacks = 0
        self.close_calls = 0
        self.last_query: str | None = None

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.close_calls += 1


def test_init_db_creates_podcasts_table() -> None:
    connection = FakeConnection()

    init_db(connection)

    assert connection.table_created is True
    assert connection.rows == {}
    assert connection.commits == 1
    assert connection.last_query == CREATE_PODCASTS_TABLE_SQL


def test_podcast_repository_lifecycle() -> None:
    connection = FakeConnection()
    repository = PodcastRepository(connection)

    repository.init_db()
    created = repository.create("product-updates")

    assert created.status == "pending"
    assert created.label == "product-updates"
    assert created.id

    running = repository.mark_running(created.id)
    assert running.status == "running"
    assert connection.rows[created.id]["started_at"] is not None
    assert connection.rows[created.id]["started_at"].tzinfo is not None

    completed = repository.mark_completed(
        created.id,
        script=PodcastScript(parts=[]),
        audio_url="https://cdn.example.com/podcasts/demo.mp3",
        cover_url="https://cdn.example.com/podcasts/demo.png",
    )

    assert completed.status == "completed"
    assert completed.script == PodcastScript(parts=[])
    assert completed.audio_url == "https://cdn.example.com/podcasts/demo.mp3"
    assert completed.cover_url == "https://cdn.example.com/podcasts/demo.png"
    assert connection.rows[created.id]["completed_at"] is not None
    assert connection.rows[created.id]["completed_at"].tzinfo is not None

    listed = repository.list()
    assert listed == [
        PodcastListItem(
            id=completed.id,
            label=completed.label,
            status=completed.status,
            audio_url=completed.audio_url,
            cover_url=completed.cover_url,
        )
    ]

    detail = repository.get_by_id(created.id)
    assert detail == completed


def test_podcast_repository_marks_failed() -> None:
    connection = FakeConnection()
    repository = PodcastRepository(connection)

    created = repository.create("episode-zero")
    repository.mark_running(created.id)

    failed = repository.mark_failed(created.id, error="no chunks found")

    assert failed.status == "failed"
    assert failed.error == "no chunks found"
    assert connection.rows[created.id]["completed_at"] is not None
    assert connection.rows[created.id]["completed_at"].tzinfo is not None
    assert repository.get_by_id(created.id) == failed


def test_podcast_repository_enforces_state_machine() -> None:
    connection = FakeConnection()
    repository = PodcastRepository(connection)

    created = repository.create("episode-one")

    with pytest.raises(ValueError):
        repository.mark_completed(
            created.id,
            script=PodcastScript(parts=[]),
            audio_url=None,
            cover_url=None,
        )

    repository.mark_running(created.id)

    with pytest.raises(ValueError):
        repository.mark_running(created.id)


def test_podcast_repository_get_by_id_returns_none_for_missing_row() -> None:
    repository = PodcastRepository(FakeConnection())

    assert repository.get_by_id("missing") is None


def test_podcast_repository_close_skips_injected_connection() -> None:
    connection = FakeConnection()
    repository = PodcastRepository(connection)

    repository.close()

    assert connection.close_calls == 0


def test_podcast_repository_close_closes_owned_connection_once(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = FakeConnection()
    monkeypatch.setattr("app.podcast_repository.create_connection", lambda: connection)

    repository = PodcastRepository()

    repository.close()
    repository.close()

    assert connection.close_calls == 1
