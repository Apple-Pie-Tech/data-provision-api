from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str | None = None

    openai_api_key: str | None = None
    slng_api_key: str | None = None
    fal_key: str | None = None

    azure_storage_account: str = "applepieingestaudio"
    azure_storage_container: str = "podcasts"
    azure_storage_connection_string: str | None = None

    podcast_max_chunks: int = Field(default=40, ge=1)
    podcast_max_script_parts: int = Field(default=12, ge=1)
    podcast_timeout_seconds: int = Field(default=120, ge=1)

    qdrant_url: str = "http://qdrant:6333"
    qdrant_api_key: str | None = None
    qdrant_collection: str = "data_provision_points"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
