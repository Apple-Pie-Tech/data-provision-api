from __future__ import annotations

from typing import Any, cast

import psycopg
from psycopg.rows import dict_row

from app.config import get_settings


CREATE_PODCASTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS podcasts (
    id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'completed', 'failed')),
    script_json JSONB,
    audio_url TEXT,
    cover_url TEXT,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ
);
""".strip()


def create_connection() -> psycopg.Connection[Any]:
    settings = get_settings()
    if not settings.database_url:
        raise RuntimeError("database_url is required")

    return psycopg.connect(settings.database_url, row_factory=cast(Any, dict_row))


def _create_tables(connection: psycopg.Connection[Any]) -> None:
    with connection.cursor() as cursor:
        cursor.execute(CREATE_PODCASTS_TABLE_SQL)


def init_db(connection: psycopg.Connection[Any] | None = None) -> None:
    if connection is None:
        with create_connection() as opened_connection:
            _create_tables(opened_connection)
            opened_connection.commit()
        return

    _create_tables(connection)
    connection.commit()


__all__ = ["CREATE_PODCASTS_TABLE_SQL", "create_connection", "init_db"]
