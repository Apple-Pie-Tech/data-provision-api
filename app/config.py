from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.blob_signing import DEFAULT_BLOB_SAS_TTL_MINUTES  # pyright: ignore[reportMissingImports]
from app.podcast_clients import (  # pyright: ignore[reportMissingImports]
    DEFAULT_AUDIO_VOICE_MODEL,
    DEFAULT_BEDROCK_SCRIPT_MAX_TOKENS,
    DEFAULT_BEDROCK_SCRIPT_MODEL,
    DEFAULT_HOST_B_VOICE_MODEL,
    DEFAULT_POLLY_ENGINE,
    DEFAULT_POLLY_SAMPLE_RATE,
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    cors_allow_origins: str = (
        "http://127.0.0.1:8081,http://localhost:8081,"
        "http://127.0.0.1:8080,http://localhost:8080"
    )

    database_url: str | None = None

    aws_region: str = "us-east-1"
    bedrock_script_model: str = DEFAULT_BEDROCK_SCRIPT_MODEL
    bedrock_script_max_tokens: int = DEFAULT_BEDROCK_SCRIPT_MAX_TOKENS
    fal_key: str | None = None

    polly_engine: str = DEFAULT_POLLY_ENGINE
    polly_sample_rate: str = DEFAULT_POLLY_SAMPLE_RATE
    polly_voice_host_a: str = DEFAULT_AUDIO_VOICE_MODEL
    polly_voice_host_b: str = DEFAULT_HOST_B_VOICE_MODEL

    azure_storage_container: str = "podcasts"
    azure_storage_connection_string: str | None = None
    # Blob containers stay private; read URLs are signed per response (E52).
    blob_sas_ttl_minutes: int = Field(default=DEFAULT_BLOB_SAS_TTL_MINUTES, ge=1)

    podcast_max_chunks: int = Field(default=40, ge=1)
    podcast_max_script_parts: int = Field(default=12, ge=1)
    podcast_timeout_seconds: int = Field(default=120, ge=1)

    qdrant_url: str = "http://qdrant:6333"
    qdrant_api_key: str | None = None
    qdrant_collection: str = "apple_pie_story_chunks"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
