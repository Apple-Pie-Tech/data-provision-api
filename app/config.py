from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.url_signing import (  # pyright: ignore[reportMissingImports]
    DEFAULT_PRESIGNED_URL_TTL_MINUTES,
)
from app.podcast_clients import (  # pyright: ignore[reportMissingImports]
    DEFAULT_AUDIO_VOICE_MODEL,
    DEFAULT_BEDROCK_SCRIPT_MAX_TOKENS,
    DEFAULT_BEDROCK_SCRIPT_MODEL,
    DEFAULT_HOST_B_VOICE_MODEL,
    DEFAULT_POLLY_ENGINE,
    DEFAULT_POLLY_SAMPLE_RATE,
    DEFAULT_PODCAST_BUCKET,
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    cors_allow_origins: str = (
        "http://127.0.0.1:8081,http://localhost:8081,"
        "http://127.0.0.1:8080,http://localhost:8080"
    )

    # Podcasts table, created by Terraform. Partition key `id`, on-demand
    # billing, no sort key: the list view is a Scan ordered in the service.
    dynamodb_podcasts_table: str = "applepie-podcasts"

    aws_region: str = "us-east-1"
    bedrock_script_model: str = DEFAULT_BEDROCK_SCRIPT_MODEL
    bedrock_script_max_tokens: int = DEFAULT_BEDROCK_SCRIPT_MAX_TOKENS
    fal_key: str | None = None

    polly_engine: str = DEFAULT_POLLY_ENGINE
    polly_sample_rate: str = DEFAULT_POLLY_SAMPLE_RATE
    polly_voice_host_a: str = DEFAULT_AUDIO_VOICE_MODEL
    polly_voice_host_b: str = DEFAULT_HOST_B_VOICE_MODEL

    # Bucket for generated podcast audio and cover art, created by Terraform.
    s3_podcast_bucket: str = DEFAULT_PODCAST_BUCKET
    # Objects stay private; read URLs are presigned per response (E52).
    presigned_url_ttl_minutes: int = Field(default=DEFAULT_PRESIGNED_URL_TTL_MINUTES, ge=1)

    podcast_max_chunks: int = Field(default=40, ge=1)
    podcast_max_script_parts: int = Field(default=12, ge=1)
    podcast_timeout_seconds: int = Field(default=120, ge=1)

    # S3 Vectors bucket and index, created by Terraform and shared with
    # data-ingestion (writer) and story-labeling-api (labeller). This service
    # only ever reads from it.
    s3_vector_bucket: str | None = None
    s3_vector_index: str = "apple-pie-story-chunks"
    vector_list_batch_size: int = 500


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
