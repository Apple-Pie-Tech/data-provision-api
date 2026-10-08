from __future__ import annotations

import json
from typing import Any

import pytest

from app.db import PARTITION_KEY, init_db  # pyright: ignore[reportMissingImports]
from app.podcast_repository import (  # pyright: ignore[reportMissingImports]
    ALLOWED_STATUSES,
    PodcastRepository,
)
from app.podcast_schemas import PodcastListItem, PodcastScript


class ConditionalCheckFailedException(Exception):
    """Mirrors the error botocore raises, which it builds dynamically per client."""

    def __init__(self) -> None:
        super().__init__("The conditional request failed")
        self.response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class FakeTable:
    """An in-memory DynamoDB table that honours the expressions under test.

    It evaluates ConditionExpression and UpdateExpression rather than recording
    them, because the state machine now lives in those expressions: a fake that
    ignored them would pass while the real transitions were unguarded.
    """

    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}
        self.close_calls = 0
        self.scan_calls = 0
        self.scan_kwargs: list[dict[str, Any]] = []
        self.update_calls: list[dict[str, Any]] = []

    def put_item(self, **kwargs: Any) -> dict[str, Any]:
        item = dict(kwargs["Item"])
        condition = kwargs.get("ConditionExpression")
        if condition == "attribute_not_exists(id)" and item["id"] in self.items:
            raise ConditionalCheckFailedException
        self.items[item["id"]] = item
        return {}

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        item = self.items.get(kwargs["Key"][PARTITION_KEY])
        return {} if item is None else {"Item": dict(item)}

    def update_item(self, **kwargs: Any) -> dict[str, Any]:
        self.update_calls.append(kwargs)
        key = kwargs["Key"][PARTITION_KEY]
        names: dict[str, str] = kwargs["ExpressionAttributeNames"]
        values: dict[str, Any] = kwargs["ExpressionAttributeValues"]
        item = self.items.get(key)

        if not self._condition_holds(kwargs["ConditionExpression"], item, names, values):
            raise ConditionalCheckFailedException

        assert item is not None
        self._apply(kwargs["UpdateExpression"], item, names, values)
        return {"Attributes": dict(item)}

    def scan(self, **kwargs: Any) -> dict[str, Any]:
        self.scan_calls += 1
        self.scan_kwargs.append(kwargs)
        names: dict[str, str] = kwargs.get("ExpressionAttributeNames", {})
        projection = kwargs.get("ProjectionExpression")
        items = list(self.items.values())
        if projection:
            wanted = {names[alias.strip()] for alias in projection.split(",")}
            items = [
                {key: value for key, value in item.items() if key in wanted}
                for item in items
            ]
        return {"Items": items}

    def close(self) -> None:
        self.close_calls += 1

    @staticmethod
    def _condition_holds(
        condition: str,
        item: dict[str, Any] | None,
        names: dict[str, str],
        values: dict[str, Any],
    ) -> bool:
        if "attribute_exists(#id)" in condition and item is None:
            return False
        if item is None:
            return False
        if "#status IN (" not in condition:
            return True
        placeholders = condition.split("#status IN (", 1)[1].rstrip(")").split(",")
        allowed = {values[placeholder.strip()] for placeholder in placeholders}
        return item.get(names["#status"]) in allowed

    @staticmethod
    def _apply(
        expression: str,
        item: dict[str, Any],
        names: dict[str, str],
        values: dict[str, Any],
    ) -> None:
        set_part, _, remove_part = expression.partition(" REMOVE ")
        for assignment in set_part.removeprefix("SET ").split(","):
            alias, _, placeholder = assignment.partition("=")
            item[names[alias.strip()]] = values[placeholder.strip()]
        for alias in filter(None, (name.strip() for name in remove_part.split(","))):
            item.pop(names[alias], None)


def make_repository() -> tuple[PodcastRepository, FakeTable]:
    table = FakeTable()
    return PodcastRepository(table), table


def test_init_db_does_not_create_the_table() -> None:
    """Terraform owns the table; the service must not need CreateTable."""
    table = FakeTable()

    init_db(table)

    assert table.items == {}
    assert table.update_calls == []


def test_allowed_statuses_are_the_four_the_state_machine_uses() -> None:
    """The Postgres CHECK constraint is gone, so this set is the only guard left."""
    assert ALLOWED_STATUSES == {"pending", "running", "completed", "failed"}


def test_podcast_repository_lifecycle() -> None:
    repository, table = make_repository()

    repository.init_db()
    created = repository.create("product-updates")

    assert created.status == "pending"
    assert created.label == "product-updates"
    assert created.id
    assert table.items[created.id]["created_at"]

    running = repository.mark_running(created.id)
    assert running.status == "running"
    assert table.items[created.id]["started_at"]

    completed = repository.mark_completed(
        created.id,
        script=PodcastScript(parts=[]),
        audio_url="https://applepie-podcasts.s3.us-east-1.amazonaws.com/p/podcast.wav",
        cover_url="https://applepie-podcasts.s3.us-east-1.amazonaws.com/p/cover.png",
    )

    assert completed.status == "completed"
    assert completed.script == PodcastScript(parts=[])
    assert completed.audio_url == (
        "https://applepie-podcasts.s3.us-east-1.amazonaws.com/p/podcast.wav"
    )
    assert completed.cover_url == (
        "https://applepie-podcasts.s3.us-east-1.amazonaws.com/p/cover.png"
    )
    assert table.items[created.id]["completed_at"]

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

    assert repository.get_by_id(created.id) == completed


def test_the_script_is_stored_as_json_and_read_back() -> None:
    repository, table = make_repository()
    created = repository.create("product-updates")
    repository.mark_running(created.id)
    script = PodcastScript.model_validate(
        {"parts": [{"speaker": "host_a", "text": "Welcome back."}]}
    )

    repository.mark_completed(created.id, script=script, audio_url=None, cover_url=None)

    stored = table.items[created.id]["script_json"]
    assert isinstance(stored, str), "the script is stored as a JSON string"
    assert json.loads(stored) == script.model_dump(mode="json")
    assert repository.get_by_id(created.id).script == script


def test_a_null_url_is_absent_rather_than_stored_as_null() -> None:
    """DynamoDB has a NULL type, but an absent attribute is the cheaper encoding."""
    repository, table = make_repository()
    created = repository.create("product-updates")
    repository.mark_running(created.id)

    repository.mark_completed(
        created.id, script=PodcastScript(parts=[]), audio_url=None, cover_url=None
    )

    assert "audio_url" not in table.items[created.id]
    assert "cover_url" not in table.items[created.id]
    assert repository.get_by_id(created.id).audio_url is None


def test_podcast_repository_marks_failed() -> None:
    repository, table = make_repository()
    created = repository.create("episode-zero")
    repository.mark_running(created.id)

    failed = repository.mark_failed(created.id, error="no chunks found")

    assert failed.status == "failed"
    assert failed.error == "no chunks found"
    assert table.items[created.id]["completed_at"]
    assert repository.get_by_id(created.id) == failed


def test_podcast_repository_marks_failed_before_the_job_ever_started() -> None:
    """A job that dies while wiring its dependencies must not stay pending (E35)."""
    repository, _ = make_repository()
    created = repository.create("episode-setup-failure")

    failed = repository.mark_failed(created.id, error="missing credentials")

    assert failed.status == "failed"
    assert failed.error == "missing credentials"


def test_completing_a_retried_podcast_clears_the_previous_error() -> None:
    """Otherwise a completed podcast carries the error from its failed attempt."""
    repository, table = make_repository()
    created = repository.create("episode-retry")
    repository.mark_failed(created.id, error="transient Bedrock failure")
    # Reset to running the way a retry would, bypassing the state machine.
    table.items[created.id]["status"] = "running"

    completed = repository.mark_completed(
        created.id, script=PodcastScript(parts=[]), audio_url=None, cover_url=None
    )

    assert completed.error is None
    assert "error" not in table.items[created.id]


def test_podcast_repository_enforces_state_machine() -> None:
    repository, _ = make_repository()
    created = repository.create("episode-one")

    with pytest.raises(ValueError):
        repository.mark_completed(
            created.id, script=PodcastScript(parts=[]), audio_url=None, cover_url=None
        )

    repository.mark_running(created.id)

    with pytest.raises(ValueError):
        repository.mark_running(created.id)


def test_the_state_machine_is_enforced_by_the_condition_not_a_prior_read() -> None:
    """The guard must be atomic, which it was not under Postgres.

    read-then-write let two concurrent attempts both see `pending` and both
    mark the job running. The condition now travels with the write, so the
    transition is rejected by DynamoDB rather than by a racing read.
    """
    repository, table = make_repository()
    created = repository.create("episode-one")
    repository.mark_running(created.id)
    table.update_calls.clear()

    with pytest.raises(ValueError):
        repository.mark_running(created.id)

    assert len(table.update_calls) == 1, "the transition was attempted, not pre-checked"
    condition = table.update_calls[0]["ConditionExpression"]
    assert "#status IN (" in condition
    assert "attribute_exists(#id)" in condition


def test_a_transition_on_a_missing_podcast_raises_key_error() -> None:
    """The routes map KeyError to a 404 and ValueError to a 409."""
    repository, _ = make_repository()

    with pytest.raises(KeyError):
        repository.mark_running("does-not-exist")


def test_podcast_repository_get_by_id_returns_none_for_missing_row() -> None:
    repository, _ = make_repository()

    assert repository.get_by_id("missing") is None


def test_list_returns_newest_first() -> None:
    """A Scan is unordered, so the created_at ordering is restored in the service."""
    repository, table = make_repository()
    first = repository.create("oldest")
    second = repository.create("newest")
    table.items[first.id]["created_at"] = "2026-01-01T00:00:00+00:00"
    table.items[second.id]["created_at"] = "2026-06-01T00:00:00+00:00"

    assert [item.label for item in repository.list()] == ["newest", "oldest"]


def test_list_follows_scan_pagination() -> None:
    """A Scan returns one page at a time; dropping LastEvaluatedKey loses rows."""
    repository, table = make_repository()
    repository.create("one")
    repository.create("two")
    pages = [
        {"Items": list(table.items.values())[:1], "LastEvaluatedKey": {"id": "cursor"}},
        {"Items": list(table.items.values())[1:]},
    ]
    calls: list[dict[str, Any]] = []

    def paged_scan(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return pages[len(calls) - 1]

    table.scan = paged_scan  # type: ignore[method-assign]

    assert len(repository.list()) == 2
    assert len(calls) == 2
    assert calls[1]["ExclusiveStartKey"] == {"id": "cursor"}


def test_list_projects_reserved_attribute_names_through_aliases() -> None:
    """`status` is a DynamoDB reserved word; projecting it raw is a 400.

    Only a live call would otherwise catch it, so the projection is asserted
    to go through ExpressionAttributeNames rather than naming attributes
    directly.
    """
    repository, table = make_repository()
    repository.create("one")

    repository.list()

    kwargs = table.scan_kwargs[0]
    projection = kwargs["ProjectionExpression"]
    names = kwargs["ExpressionAttributeNames"]
    assert "status" not in projection.replace("#status", "")
    assert names["#status"] == "status"
    assert all(alias.strip() in names for alias in projection.split(","))


def test_podcast_repository_close_skips_injected_table() -> None:
    repository, table = make_repository()

    repository.close()

    assert table.close_calls == 0


def test_podcast_repository_close_closes_owned_table_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    table = FakeTable()
    monkeypatch.setattr("app.podcast_repository.create_table", lambda: table)

    repository = PodcastRepository()

    repository.close()
    repository.close()

    assert table.close_calls == 1
