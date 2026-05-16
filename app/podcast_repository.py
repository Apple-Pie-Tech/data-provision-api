from __future__ import annotations

from collections.abc import Mapping
import json
from typing import Any, cast
from uuid import uuid4

from app.db import create_connection, init_db  # pyright: ignore[reportMissingImports]
from app.podcast_schemas import PodcastDetail, PodcastListItem, PodcastScript, PodcastStatus
from psycopg.abc import QueryNoTemplate


PENDING = "pending"
RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"

ALLOWED_STATUSES = {PENDING, RUNNING, COMPLETED, FAILED}


class PodcastRepository:
    def __init__(self, connection: Any | None = None) -> None:
        self._owns_connection = connection is None
        self._connection = connection or create_connection()
        self._closed = False

    def close(self) -> None:
        if self._closed or not self._owns_connection:
            return

        close = getattr(self._connection, "close", None)
        if callable(close):
            close()
        self._closed = True

    def init_db(self) -> None:
        init_db(self._connection)

    def create(self, label: str) -> PodcastDetail:
        podcast_id = str(uuid4())
        row = self._write_and_return_one(
            """
            INSERT INTO podcasts (
                id,
                label,
                status,
                script_json,
                audio_url,
                cover_url,
                error
            )
            VALUES (%s, %s, %s, NULL, NULL, NULL, NULL)
            RETURNING *
            """.strip(),
            (podcast_id, label, PENDING),
        )
        return self._to_detail(row)

    def mark_running(self, podcast_id: str) -> PodcastDetail:
        self._require_status(podcast_id, PENDING)
        row = self._write_and_return_one(
            """
            UPDATE podcasts
            SET status = %s,
                started_at = NOW(),
                updated_at = NOW()
            WHERE id = %s
            RETURNING *
            """.strip(),
            (RUNNING, podcast_id),
        )
        return self._to_detail(row)

    def mark_completed(
        self,
        podcast_id: str,
        *,
        script: PodcastScript,
        audio_url: str | None,
        cover_url: str | None,
    ) -> PodcastDetail:
        self._require_status(podcast_id, RUNNING)
        row = self._write_and_return_one(
            """
            UPDATE podcasts
            SET status = %s,
                script_json = %s::jsonb,
                audio_url = %s,
                cover_url = %s,
                error = NULL,
                completed_at = NOW(),
                updated_at = NOW()
            WHERE id = %s
            RETURNING *
            """.strip(),
            (
                COMPLETED,
                json.dumps(script.model_dump(mode="json")),
                audio_url,
                cover_url,
                podcast_id,
            ),
        )
        return self._to_detail(row)

    def mark_failed(self, podcast_id: str, *, error: str) -> PodcastDetail:
        self._require_status(podcast_id, RUNNING)
        row = self._write_and_return_one(
            """
            UPDATE podcasts
            SET status = %s,
                error = %s,
                completed_at = NOW(),
                updated_at = NOW()
            WHERE id = %s
            RETURNING *
            """.strip(),
            (FAILED, error, podcast_id),
        )
        return self._to_detail(row)

    def list(self) -> list[PodcastListItem]:
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, label, status, audio_url, cover_url
                FROM podcasts
                ORDER BY created_at DESC, id DESC
                """.strip()
            )
            rows = cursor.fetchall()
        return [self._to_list_item(row) for row in rows]

    def get_by_id(self, podcast_id: str) -> PodcastDetail | None:
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, label, status, script_json, audio_url, cover_url, error,
                       created_at, updated_at, started_at, completed_at
                FROM podcasts
                WHERE id = %s
                """.strip(),
                (podcast_id,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return self._to_detail(row)

    def _require_status(self, podcast_id: str, expected_status: str) -> None:
        current = self.get_by_id(podcast_id)
        if current is None:
            raise KeyError(podcast_id)
        if current.status != expected_status:
            raise ValueError(f"podcast {podcast_id} must be {expected_status} before this transition")

    def _write_and_return_one(self, query: str, params: tuple[Any, ...]) -> Mapping[str, Any]:
        try:
            with self._connection.cursor() as cursor:
                cursor.execute(cast(QueryNoTemplate, query), params)
                row = cursor.fetchone()
            self._connection.commit()
            if row is None:
                raise KeyError("podcast row was not returned")
            return row
        except Exception:
            rollback = getattr(self._connection, "rollback", None)
            if callable(rollback):
                rollback()
            raise

    def _to_detail(self, row: Mapping[str, Any]) -> PodcastDetail:
        return PodcastDetail(
            id=str(self._value(row, "id")),
            label=str(self._value(row, "label")),
            status=cast(PodcastStatus, str(self._value(row, "status"))),
            audio_url=self._optional_str(row, "audio_url"),
            cover_url=self._optional_str(row, "cover_url"),
            script=self._script_from_row(row),
            error=self._optional_str(row, "error"),
        )

    def _to_list_item(self, row: Mapping[str, Any]) -> PodcastListItem:
        return PodcastListItem(
            id=str(self._value(row, "id")),
            label=str(self._value(row, "label")),
            status=cast(PodcastStatus, str(self._value(row, "status"))),
            audio_url=self._optional_str(row, "audio_url"),
            cover_url=self._optional_str(row, "cover_url"),
        )

    def _script_from_row(self, row: Mapping[str, Any]) -> PodcastScript | None:
        raw_script = self._value(row, "script_json", default=None)
        if raw_script is None:
            return None
        if isinstance(raw_script, str):
            raw_script = json.loads(raw_script)
        return PodcastScript.model_validate(raw_script)

    def _value(self, row: Mapping[str, Any], key: str, *, default: Any = ...):
        if isinstance(row, Mapping):
            if default is ...:
                return row[key]
            return row.get(key, default)

        if default is ...:
            return getattr(row, key)
        return getattr(row, key, default)

    def _optional_str(self, row: Mapping[str, Any], key: str) -> str | None:
        value = self._value(row, key, default=None)
        if value is None:
            return None
        return str(value)


__all__ = ["ALLOWED_STATUSES", "COMPLETED", "FAILED", "PENDING", "PodcastRepository", "RUNNING"]
