from __future__ import annotations

from importlib import import_module
from typing import Any, Protocol

from app.config import get_settings


PARTITION_KEY = "id"

# Attribute names used in expressions must be aliased when DynamoDB reserves
# them. `status` is reserved; the rest are aliased too so adding an attribute
# cannot quietly hit the reserved-word list.
ITEM_ATTRIBUTES: tuple[str, ...] = (
    "id",
    "label",
    "status",
    "script_json",
    "audio_url",
    "cover_url",
    "error",
    "created_at",
    "updated_at",
    "started_at",
    "completed_at",
)


class SupportsTable(Protocol):
    """The subset of boto3's DynamoDB Table resource this service uses."""

    def get_item(self, **kwargs: Any) -> dict[str, Any]: ...

    def put_item(self, **kwargs: Any) -> dict[str, Any]: ...

    def update_item(self, **kwargs: Any) -> dict[str, Any]: ...

    def scan(self, **kwargs: Any) -> dict[str, Any]: ...


def create_table() -> Any:
    """Return the podcasts table resource.

    The `resource` interface rather than the low-level client, so items are
    plain Python dicts instead of AttributeValue wrappers. Every attribute this
    service stores is a string, so none of the resource layer's Decimal
    behaviour comes into play.
    """
    settings = get_settings()
    if not settings.dynamodb_podcasts_table:
        raise RuntimeError("dynamodb_podcasts_table is required")

    boto3 = import_module("boto3")
    resource = boto3.resource("dynamodb", region_name=settings.aws_region)
    return resource.Table(settings.dynamodb_podcasts_table)


def init_db(table: Any | None = None) -> None:
    """Deliberately does nothing.

    The table is created by Terraform. It used to be `CREATE TABLE IF NOT
    EXISTS`, which meant every request opened a connection and issued DDL; the
    equivalent here would be CreateTable, and a service role that can create
    tables is a wider grant than this service has any use for. A missing table
    surfaces as DynamoDB's own ResourceNotFoundException on first use, which
    names the table.

    Kept as a no-op rather than removed because it is the request lifecycle's
    hook for schema setup, and callers should not have to care which backend
    needs one.
    """
    return None


__all__ = [
    "ITEM_ATTRIBUTES",
    "PARTITION_KEY",
    "SupportsTable",
    "create_table",
    "init_db",
]
