from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.secrets import resolve_secret_fields  # pyright: ignore[reportMissingImports]

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

    # Checked against the `x-api-key` header on POST /podcasts, mirroring
    # data-ingestion's INGEST_API_KEY. Blank disables the check.
    provision_api_key: str | None = None
    provision_api_key_secret_arn: str | None = None

    # Verifying the Supabase access token the UI already holds is what makes
    # the write endpoint authenticated rather than merely deterred. Unset means
    # the check is SKIPPED, matching PROVISION_API_KEY above and for the same reason:
    # a fresh clone with no .env has to keep working. Terraform always sets it,
    # and create_app warns when it is missing so an open deployment is not
    # silent.
    supabase_url: str | None = None
    supabase_jwt_audience: str = "authenticated"
    # Only for a project still signing with the legacy HS256 shared secret. In
    # the asymmetric regime (ES256/RS256) the keys are published at the
    # project's JWKS endpoint and there is no secret to hold at all.
    supabase_jwt_secret: str | None = None
    supabase_jwt_secret_arn: str | None = None
    supabase_jwks_cache_seconds: int = 600

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
    # Set by Terraform instead of the value above: a secret value in a Lambda
    # environment variable would be recorded in Terraform state in plaintext.
    # Resolved once by get_settings(); the plain field still works locally.
    fal_key_secret_arn: str | None = None

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


# Target field -> the setting holding the ARN to resolve it from.
SECRET_FIELD_ARNS = {
    "fal_key": "fal_key_secret_arn",
    "provision_api_key": "provision_api_key_secret_arn",
    "supabase_jwt_secret": "supabase_jwt_secret_arn",
}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Build Settings, resolving any Secrets Manager ARNs it was given.

    Cached, so the Secrets Manager call happens once per execution environment
    rather than once per request.
    """
    settings = Settings()
    resolved = resolve_secret_fields(
        settings,
        SECRET_FIELD_ARNS,
        region=settings.aws_region,
    )
    if not resolved:
        return settings
    return settings.model_copy(update=resolved)
