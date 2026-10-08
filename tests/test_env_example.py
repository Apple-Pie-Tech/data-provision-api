from pathlib import Path


ENV_EXAMPLE_PATH = Path(__file__).resolve().parents[1] / ".env.example"


def _parse_env_assignments(content: str) -> dict[str, str]:
    assignments: dict[str, str] = {}

    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue

        key, value = stripped.split("=", 1)
        assignments[key] = value

    return assignments


def _assignment_lines(content: str) -> list[str]:
    lines: list[str] = []

    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue

        lines.append(stripped)

    return lines


def test_env_example_documents_azure_setup_and_runtime_caveats() -> None:
    content = ENV_EXAMPLE_PATH.read_text(encoding="utf-8")

    assert "Azure PostgreSQL Flexible Server" in content
    assert "az postgres flexible-server create" in content
    assert (
        "DATABASE_URL=postgresql://<user>:<password>@<server>.postgres.database.azure.com:5432/<database>?sslmode=require"
        in content
    )
    assert "Background podcast jobs are best-effort only." in content
    assert "not durable across restarts" in content


def test_env_example_uses_placeholders_and_no_real_secrets() -> None:
    content = ENV_EXAMPLE_PATH.read_text(encoding="utf-8")
    assignments = _parse_env_assignments(content)
    env_lines = _assignment_lines(content)

    expected_blank_keys = {
        "DATABASE_URL",
        "FAL_KEY",
        "AZURE_STORAGE_CONNECTION_STRING",
        "QDRANT_API_KEY",
    }

    for key in expected_blank_keys:
        assert key in assignments
        assert assignments[key] == ""

    # Bedrock and Polly authenticate with the execution role, so there is no key
    # to document -- and no AWS_SECRET_ACCESS_KEY may ever appear here.
    assert "AWS_ACCESS_KEY_ID" not in assignments
    assert "AWS_SECRET_ACCESS_KEY" not in assignments
    assert assignments["AWS_REGION"] == "us-east-1"
    assert assignments["BEDROCK_SCRIPT_MODEL"] == "amazon.nova-pro-v1:0"
    assert assignments["POLLY_ENGINE"] == "generative"
    assert assignments["POLLY_SAMPLE_RATE"] == "16000"
    assert assignments["POLLY_VOICE_HOST_A"] == "Ruth"
    assert assignments["POLLY_VOICE_HOST_B"] == "Matthew"
    assert "AZURE_STORAGE_ACCOUNT" not in assignments
    assert assignments["AZURE_STORAGE_CONTAINER"] == "podcasts"
    assert assignments["QDRANT_URL"] == "http://qdrant:6333"
    assert assignments["QDRANT_COLLECTION"] == "apple_pie_story_chunks"

    forbidden_secret_markers = (
        "DefaultEndpointsProtocol=",
        "AccountName=",
        "AccountKey=",
        "SharedAccessSignature=",
        "postgresql://",
        "postgres://",
        "sk-",
    )

    for marker in forbidden_secret_markers:
        assert all(marker not in line for line in env_lines)


def test_env_example_documents_every_setting_the_service_reads() -> None:
    """Catches .env.example drifting behind Settings.

    Without this, renaming a setting leaves the old key documented and the new one
    undocumented, and the assertions above keep passing against stale content.
    """
    from app.config import Settings

    assignments = _parse_env_assignments(ENV_EXAMPLE_PATH.read_text(encoding="utf-8"))
    missing = sorted(
        name.upper() for name in Settings.model_fields if name.upper() not in assignments
    )

    assert missing == []
