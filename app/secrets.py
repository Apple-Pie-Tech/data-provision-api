"""Resolve Secrets Manager values at startup.

Terraform passes the secret's ARN in the environment, never its value: a value
in a Lambda environment variable would be written to Terraform state in
plaintext, which is the whole reason the secrets live in Secrets Manager. The
service therefore resolves them itself, once per process.

A failure to resolve is logged and treated as "unset" rather than raised. The
alternative is a service that cannot start, and the unset path is already
handled: a missing fal.ai key leaves `cover_generator` as None, and podcast
generation falls back to FALLBACK_COVER_PNG with `cover_used_fallback` set,
which is the behaviour this service already has for a cover that fails.
"""

from __future__ import annotations

import logging
from importlib import import_module
from typing import Any

logger = logging.getLogger(__name__)


def fetch_secret(arn: str, *, region: str, client: Any | None = None) -> str | None:
    """Return a Secrets Manager secret's string value, or None if unavailable."""

    if client is None:
        try:
            boto3 = import_module("boto3")
            client = boto3.client("secretsmanager", region_name=region)
        except Exception as exc:  # noqa: BLE001 - never fail startup over this
            logger.warning("could not build a Secrets Manager client: %s", exc)
            return None

    try:
        response = client.get_secret_value(SecretId=arn)
    except Exception as exc:  # noqa: BLE001
        # Includes the ordinary case of a secret that exists with no version
        # yet, which Terraform creates deliberately.
        logger.warning("could not resolve secret %s: %s", arn, exc)
        return None

    value = response.get("SecretString")
    if not isinstance(value, str) or not value.strip():
        logger.warning("secret %s has no usable string value", arn)
        return None

    return value


def resolve_secret_fields(
    settings: Any,
    field_arns: dict[str, str],
    *,
    region: str,
    client: Any | None = None,
) -> dict[str, str]:
    """Resolve `{target_field: arn_field}` into `{target_field: value}`.

    Only fields whose ARN is set and whose secret resolved are returned, so the
    caller can apply them as overrides without clobbering a value that came from
    the environment directly -- which is how local development keeps working.
    """

    resolved: dict[str, str] = {}
    for target_field, arn_field in field_arns.items():
        arn = getattr(settings, arn_field, None)
        if not arn:
            continue
        value = fetch_secret(arn, region=region, client=client)
        if value is not None:
            resolved[target_field] = value
    return resolved


__all__ = ["fetch_secret", "resolve_secret_fields"]
