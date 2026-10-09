from __future__ import annotations

from typing import Any

from app.config import SECRET_FIELD_ARNS, Settings
from app.secrets import fetch_secret, resolve_secret_fields


class FakeSecretsManager:
    def __init__(self, values: dict[str, Any] | None = None, *, error: Exception | None = None) -> None:
        self.values = values or {}
        self.error = error
        self.calls: list[str] = []

    def get_secret_value(self, *, SecretId: str) -> dict[str, Any]:
        self.calls.append(SecretId)
        if self.error is not None:
            raise self.error
        if SecretId not in self.values:
            raise KeyError(SecretId)
        return self.values[SecretId]


def test_fetch_secret_returns_the_string_value() -> None:
    client = FakeSecretsManager({"arn:a": {"SecretString": "gsk-live-value"}})

    assert fetch_secret("arn:a", region="us-east-1", client=client) == "gsk-live-value"
    assert client.calls == ["arn:a"]


def test_a_secret_with_no_version_resolves_to_none() -> None:
    """Terraform creates the container without a version, on purpose.

    The value is set out of band, so "exists but empty" is a normal state and
    must not take the service down.
    """
    client = FakeSecretsManager(error=RuntimeError("ResourceNotFoundException"))

    assert fetch_secret("arn:a", region="us-east-1", client=client) is None


def test_a_binary_only_secret_resolves_to_none() -> None:
    client = FakeSecretsManager({"arn:a": {"SecretBinary": b"\x00"}})

    assert fetch_secret("arn:a", region="us-east-1", client=client) is None


def test_a_blank_secret_resolves_to_none() -> None:
    client = FakeSecretsManager({"arn:a": {"SecretString": "   "}})

    assert fetch_secret("arn:a", region="us-east-1", client=client) is None


def test_resolve_skips_fields_with_no_arn() -> None:
    """Local development sets the plain env var and no ARN; it must be left alone."""
    settings = Settings(_env_file=None, fal_key="from-env")
    client = FakeSecretsManager()

    resolved = resolve_secret_fields(
        settings, SECRET_FIELD_ARNS, region="us-east-1", client=client
    )

    assert resolved == {}
    assert client.calls == []


def test_resolve_returns_the_resolved_field() -> None:
    settings = Settings(_env_file=None, fal_key_secret_arn="arn:fal")
    client = FakeSecretsManager({"arn:fal": {"SecretString": "fal-value"}})

    resolved = resolve_secret_fields(
        settings, SECRET_FIELD_ARNS, region="us-east-1", client=client
    )

    assert resolved == {"fal_key": "fal-value"}


def test_an_unresolvable_secret_is_absent_rather_than_none() -> None:
    """The caller applies these as overrides, so a None would clobber a real value."""
    settings = Settings(_env_file=None, fal_key="from-env", fal_key_secret_arn="arn:fal")
    client = FakeSecretsManager(error=RuntimeError("ResourceNotFoundException"))

    resolved = resolve_secret_fields(
        settings, SECRET_FIELD_ARNS, region="us-east-1", client=client
    )

    assert resolved == {}


def test_every_secret_field_names_a_real_setting() -> None:
    """Guards against a rename leaving SECRET_FIELD_ARNS pointing at nothing.

    A typo here is silent: the ARN is never read and the secret never resolves.
    """
    fields = Settings.model_fields
    for target_field, arn_field in SECRET_FIELD_ARNS.items():
        assert target_field in fields, target_field
        assert arn_field in fields, arn_field
