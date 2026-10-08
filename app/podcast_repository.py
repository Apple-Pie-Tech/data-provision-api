from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

from app.db import create_table, init_db  # pyright: ignore[reportMissingImports]
from app.podcast_schemas import PodcastDetail, PodcastListItem, PodcastScript, PodcastStatus


PENDING = "pending"
RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"

ALLOWED_STATUSES = {PENDING, RUNNING, COMPLETED, FAILED}

# Attributes the list view needs. `status` is a DynamoDB reserved word, so the
# projection goes through ExpressionAttributeNames; created_at is fetched only
# to order the results and is not exposed.
_LIST_ATTRIBUTES: tuple[str, ...] = (
    "id",
    "label",
    "status",
    "audio_url",
    "cover_url",
    "created_at",
)


def _now() -> str:
    """An ISO-8601 UTC timestamp.

    Postgres stamped these with NOW() server-side. DynamoDB has no equivalent,
    so they are generated here; stored as strings because that sorts
    lexicographically in the same order it sorts chronologically.
    """
    return datetime.now(UTC).isoformat()


def _is_conditional_check_failure(exc: BaseException) -> bool:
    """Recognise a failed ConditionExpression without importing botocore.

    botocore builds ConditionalCheckFailedException dynamically per client, so
    there is no stable class to catch; the error code is the stable part, and
    checking it by name also lets an injected fake raise something equivalent.
    """
    code = getattr(exc, "response", {}).get("Error", {}).get("Code")
    if code == "ConditionalCheckFailedException":
        return True
    return type(exc).__name__ == "ConditionalCheckFailedException"


class PodcastRepository:
    """Podcast rows in DynamoDB, keyed by `id`.

    The status state machine used to be a Postgres CHECK constraint plus a
    read-then-write guard in this class, which left a race: two concurrent
    attempts could both read `pending` and both mark the job running. Each
    transition is now a single conditional update, so the condition is checked
    and the write applied atomically.
    """

    def __init__(self, table: Any | None = None) -> None:
        self._owns_table = table is None
        self._table = table if table is not None else create_table()
        self._closed = False

    def close(self) -> None:
        """Release the table resource, if it owns one and it has anything to release.

        DynamoDB over HTTP has no connection to close, unlike the psycopg
        connection this replaces, so for the real table this is a no-op. The
        method stays because the request lifecycle calls it and an injected
        double may well want to observe it.
        """
        if self._closed or not self._owns_table:
            return

        close = getattr(self._table, "close", None)
        if callable(close):
            close()
        self._closed = True

    def init_db(self) -> None:
        init_db(self._table)

    def create(self, label: str) -> PodcastDetail:
        podcast_id = str(uuid4())
        now = _now()
        item = {
            "id": podcast_id,
            "label": label,
            "status": PENDING,
            "created_at": now,
            "updated_at": now,
        }
        # Guards against a uuid4 collision rather than expecting one: without
        # it, put_item would silently overwrite an existing podcast.
        self._table.put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(id)",
        )
        return self._to_detail(item)

    def mark_running(self, podcast_id: str) -> PodcastDetail:
        return self._transition(
            podcast_id,
            expected_statuses=(PENDING,),
            values={"status": RUNNING, "started_at": _now()},
        )

    def mark_completed(
        self,
        podcast_id: str,
        *,
        script: PodcastScript,
        audio_url: str | None,
        cover_url: str | None,
    ) -> PodcastDetail:
        return self._transition(
            podcast_id,
            expected_statuses=(RUNNING,),
            values={
                "status": COMPLETED,
                "script_json": json.dumps(script.model_dump(mode="json")),
                "audio_url": audio_url,
                "cover_url": cover_url,
                "completed_at": _now(),
            },
            # A retry after a failure must not leave the old error attached to
            # a now-completed podcast.
            remove=("error",),
        )

    def mark_failed(self, podcast_id: str, *, error: str) -> PodcastDetail:
        # A job can die before it ever starts - e.g. while building its
        # dependencies - so PENDING is a legitimate source state here (E35).
        return self._transition(
            podcast_id,
            expected_statuses=(PENDING, RUNNING),
            values={"status": FAILED, "error": error, "completed_at": _now()},
        )

    def list(self) -> list[PodcastListItem]:
        """Every podcast, newest first.

        A Scan, which is what DynamoDB offers without a sort key, and the order
        is restored here. Fine at this scale - a handful of rows - and the fix
        if it ever stops being is a GSI with created_at as its sort key, not a
        bigger Scan.
        """
        items: list[Mapping[str, Any]] = []
        kwargs: dict[str, Any] = {
            "ProjectionExpression": ", ".join(f"#{name}" for name in _LIST_ATTRIBUTES),
            "ExpressionAttributeNames": {f"#{name}": name for name in _LIST_ATTRIBUTES},
        }
        while True:
            response = self._table.scan(**kwargs)
            items.extend(response.get("Items") or [])
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                break
            kwargs["ExclusiveStartKey"] = last_key

        items.sort(
            key=lambda item: (str(item.get("created_at") or ""), str(item.get("id") or "")),
            reverse=True,
        )
        return [self._to_list_item(item) for item in items]

    def get_by_id(self, podcast_id: str) -> PodcastDetail | None:
        response = self._table.get_item(Key={"id": podcast_id})
        item = response.get("Item")
        if item is None:
            return None
        return self._to_detail(item)

    def _transition(
        self,
        podcast_id: str,
        *,
        expected_statuses: Sequence[str],
        values: Mapping[str, Any],
        remove: Sequence[str] = (),
    ) -> PodcastDetail:
        """Apply one state-machine transition as a single conditional update."""
        # A None means "no value", which is an absent attribute in DynamoDB
        # rather than the SQL NULL the previous implementation wrote.
        set_values = {key: value for key, value in values.items() if value is not None}
        remove_names = tuple(remove) + tuple(
            key for key, value in values.items() if value is None
        )
        set_values["updated_at"] = _now()

        names = {f"#{name}": name for name in (*set_values, *remove_names, "status", "id")}
        attribute_values = {f":{name}": value for name, value in set_values.items()}

        expression = "SET " + ", ".join(f"#{name} = :{name}" for name in set_values)
        if remove_names:
            expression += " REMOVE " + ", ".join(f"#{name}" for name in remove_names)

        status_placeholders: list[str] = []
        for index, status in enumerate(expected_statuses):
            placeholder = f":expected_status_{index}"
            attribute_values[placeholder] = status
            status_placeholders.append(placeholder)

        condition = (
            f"attribute_exists(#id) AND #status IN ({', '.join(status_placeholders)})"
        )

        try:
            response = self._table.update_item(
                Key={"id": podcast_id},
                UpdateExpression=expression,
                ConditionExpression=condition,
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=attribute_values,
                ReturnValues="ALL_NEW",
            )
        except Exception as exc:
            if _is_conditional_check_failure(exc):
                self._raise_transition_error(podcast_id, expected_statuses)
            raise

        attributes = response.get("Attributes")
        if attributes is None:
            raise KeyError(f"podcast {podcast_id} was not returned by the update")
        return self._to_detail(attributes)

    def _raise_transition_error(
        self,
        podcast_id: str,
        expected_statuses: Iterable[str],
    ) -> None:
        """Translate a failed condition into the error the callers already handle.

        The condition covers both "no such podcast" and "wrong status", so the
        item is read to tell them apart: the routes map KeyError to a 404 and
        ValueError to a 409.
        """
        current = self.get_by_id(podcast_id)
        if current is None:
            raise KeyError(podcast_id)

        expected = " or ".join(sorted(expected_statuses))
        raise ValueError(f"podcast {podcast_id} must be {expected} before this transition")

    def _to_detail(self, item: Mapping[str, Any]) -> PodcastDetail:
        return PodcastDetail(
            id=str(item["id"]),
            label=str(item["label"]),
            status=cast(PodcastStatus, str(item["status"])),
            audio_url=self._optional_str(item, "audio_url"),
            cover_url=self._optional_str(item, "cover_url"),
            script=self._script_from_item(item),
            error=self._optional_str(item, "error"),
        )

    def _to_list_item(self, item: Mapping[str, Any]) -> PodcastListItem:
        return PodcastListItem(
            id=str(item["id"]),
            label=str(item["label"]),
            status=cast(PodcastStatus, str(item["status"])),
            audio_url=self._optional_str(item, "audio_url"),
            cover_url=self._optional_str(item, "cover_url"),
        )

    def _script_from_item(self, item: Mapping[str, Any]) -> PodcastScript | None:
        raw_script = item.get("script_json")
        if raw_script is None:
            return None
        if isinstance(raw_script, str):
            raw_script = json.loads(raw_script)
        return PodcastScript.model_validate(raw_script)

    def _optional_str(self, item: Mapping[str, Any], key: str) -> str | None:
        value = item.get(key)
        if value is None:
            return None
        return str(value)


__all__ = [
    "ALLOWED_STATUSES",
    "COMPLETED",
    "FAILED",
    "PENDING",
    "RUNNING",
    "PodcastRepository",
]
