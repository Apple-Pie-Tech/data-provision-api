from pydantic import BaseModel, ConfigDict, Field


class UniversePoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    label: str
    audio_url: str | None = None
    is_synthetic: bool = False
    is_central: bool = False


class UniverseEdge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str
    target_id: str


class UniverseResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    points: list[UniversePoint] = Field(default_factory=list)
    edges: list[UniverseEdge] = Field(default_factory=list)


__all__ = ["UniverseEdge", "UniversePoint", "UniverseResponse"]
