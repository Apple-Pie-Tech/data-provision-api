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
    assert "az storage container create --account-name applepieingestaudio --name podcasts --auth-mode login" in content
    assert "Background podcast jobs are best-effort only." in content
    assert "not durable across restarts" in content


def test_env_example_uses_placeholders_and_no_real_secrets() -> None:
    content = ENV_EXAMPLE_PATH.read_text(encoding="utf-8")
    assignments = _parse_env_assignments(content)
    env_lines = _assignment_lines(content)

    expected_blank_keys = {
        "DATABASE_URL",
        "OPENAI_API_KEY",
        "SLNG_API_KEY",
        "FAL_KEY",
        "AZURE_STORAGE_CONNECTION_STRING",
        "QDRANT_API_KEY",
    }

    for key in expected_blank_keys:
        assert key in assignments
        assert assignments[key] == ""

    assert assignments["AZURE_STORAGE_ACCOUNT"] == "applepieingestaudio"
    assert assignments["AZURE_STORAGE_CONTAINER"] == "podcasts"
    assert assignments["QDRANT_URL"] == "http://qdrant:6333"
    assert assignments["QDRANT_COLLECTION"] == "data_provision_points"

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
