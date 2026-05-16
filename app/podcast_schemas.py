from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


PodcastStatus = Literal["pending", "running", "completed", "failed"]


class PodcastCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class PodcastScriptLine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    speaker: str
    text: str


class PodcastScript(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parts: list[PodcastScriptLine] = Field(default_factory=list)


class PodcastListItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    label: str
    status: PodcastStatus
    audio_url: str | None = None
    cover_url: str | None = None


class PodcastDetail(PodcastListItem):
    script: PodcastScript | None = None
    error: str | None = None


__all__ = [
    "PodcastCreateRequest",
    "PodcastDetail",
    "PodcastListItem",
    "PodcastScript",
    "PodcastScriptLine",
    "PodcastStatus",
]
