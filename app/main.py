from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from qdrant_client import AsyncQdrantClient

from app.config import Settings, get_settings
from app.podcast_clients import (  # pyright: ignore[reportMissingImports]
    AzurePodcastBlobStore,
    FalCoverGenerator,
    OpenAIScriptGenerator,
    SlngTTSClient,
)
from app.podcast_generation import (  # pyright: ignore[reportMissingImports]
    AudioMerger,
    SupportsBlobStore,
    SupportsCoverGenerator,
    SupportsPointReader,
    SupportsScriptGenerator,
    SupportsTTSClient,
    generate_podcast,
)
from app.podcast_repository import PodcastRepository  # pyright: ignore[reportMissingImports]
from app.podcast_schemas import (  # pyright: ignore[reportMissingImports]
    PodcastCreateRequest,
    PodcastDetail,
    PodcastListItem,
)
from app.schemas import UniverseResponse  # pyright: ignore[reportMissingImports]
from app.universe import assemble_universe_graph  # pyright: ignore[reportMissingImports]
from app.vector_store import QdrantPointReader  # pyright: ignore[reportMissingImports]


def _parse_cors_allow_origins(raw_value: str) -> list[str]:
    return [origin for origin in (item.strip() for item in raw_value.split(",")) if origin]


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or get_settings()
    application = FastAPI(title="Data Provision API", version="0.1.0")

    cors_allow_origins = _parse_cors_allow_origins(resolved_settings.cors_allow_origins)
    if cors_allow_origins:
        application.add_middleware(
            CORSMiddleware,
            allow_credentials=False,
            allow_headers=["*"],
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_origins=cors_allow_origins,
        )

    return application


app = create_app()


@dataclass(slots=True)
class PodcastGenerationDependencies:
    point_reader: SupportsPointReader
    script_generator: SupportsScriptGenerator
    tts_client: SupportsTTSClient
    blob_store: SupportsBlobStore
    cover_generator: SupportsCoverGenerator | None
    audio_merger: AudioMerger | None


def build_podcast_generation_dependencies(
    *,
    settings: Settings,
    point_reader: SupportsPointReader,
) -> PodcastGenerationDependencies:
    try:
        cover_generator: SupportsCoverGenerator | None = FalCoverGenerator(
            timeout_seconds=settings.podcast_timeout_seconds
        )
    except Exception:
        cover_generator = None

    return PodcastGenerationDependencies(
        point_reader=point_reader,
        script_generator=OpenAIScriptGenerator(
            model="gpt-4o-mini",
            timeout_seconds=settings.podcast_timeout_seconds,
            max_parts=settings.podcast_max_script_parts,
        ),
        tts_client=SlngTTSClient(
            api_key=settings.slng_api_key,
            timeout_seconds=settings.podcast_timeout_seconds,
        ),
        blob_store=AzurePodcastBlobStore(
            connection_string=settings.azure_storage_connection_string,
            container_name=settings.azure_storage_container,
            timeout_seconds=settings.podcast_timeout_seconds,
        ),
        cover_generator=cover_generator,
        audio_merger=None,
    )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


async def get_point_reader(
    settings: Settings = Depends(get_settings),
) -> AsyncIterator[QdrantPointReader]:
    client = AsyncQdrantClient(
        url=settings.qdrant_url,
        api_key=settings.qdrant_api_key,
    )
    try:
        yield QdrantPointReader(client, settings.qdrant_collection)
    finally:
        await client.close()


def get_podcast_repository() -> Iterator[PodcastRepository]:
    repository = PodcastRepository()
    try:
        repository.init_db()
        yield repository
    finally:
        repository.close()


def get_podcast_generation_dependencies() -> PodcastGenerationDependencies | None:
    return None


async def run_podcast_generation(
    podcast_id: str,
    repository: PodcastRepository,
    generation: PodcastGenerationDependencies,
    settings: Settings,
) -> None:
    await generate_podcast(
        podcast_id,
        repository=repository,
        point_reader=generation.point_reader,
        script_generator=generation.script_generator,
        tts_client=generation.tts_client,
        blob_store=generation.blob_store,
        cover_generator=generation.cover_generator,
        audio_merger=generation.audio_merger,
        max_chunks=settings.podcast_max_chunks,
        max_script_parts=settings.podcast_max_script_parts,
        timeout_seconds=settings.podcast_timeout_seconds,
    )


async def run_podcast_generation_from_settings(
    podcast_id: str,
    settings: Settings,
) -> None:
    repository: PodcastRepository | None = None
    client = AsyncQdrantClient(
        url=settings.qdrant_url,
        api_key=settings.qdrant_api_key,
    )
    try:
        repository = PodcastRepository()
        repository.init_db()
        point_reader = QdrantPointReader(client, settings.qdrant_collection)
        generation = build_podcast_generation_dependencies(
            settings=settings,
            point_reader=point_reader,
        )
        await run_podcast_generation(podcast_id, repository, generation, settings)
    finally:
        if repository is not None:
            repository.close()
        await client.close()


@app.get("/universe", response_model=UniverseResponse)
async def get_universe(
    point_reader: QdrantPointReader = Depends(get_point_reader),
) -> UniverseResponse:
    try:
        points = await point_reader.read_points()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="vector store unavailable") from exc
    return assemble_universe_graph(points)


@app.post("/podcasts", response_model=PodcastDetail, status_code=202)
async def create_podcast(
    payload: PodcastCreateRequest,
    background_tasks: BackgroundTasks,
    repository: PodcastRepository = Depends(get_podcast_repository),
    generation: PodcastGenerationDependencies | None = Depends(get_podcast_generation_dependencies),
    settings: Settings = Depends(get_settings),
) -> PodcastDetail:
    podcast = repository.create(payload.label)
    if generation is None:
        background_tasks.add_task(
            run_podcast_generation_from_settings,
            podcast.id,
            settings,
        )
    else:
        background_tasks.add_task(
            run_podcast_generation,
            podcast.id,
            repository,
            generation,
            settings,
        )
    return podcast


@app.get("/podcasts", response_model=list[PodcastListItem])
async def list_podcasts(
    repository: PodcastRepository = Depends(get_podcast_repository),
) -> list[PodcastListItem]:
    return repository.list()


@app.get("/podcasts/{podcast_id}", response_model=PodcastDetail)
async def get_podcast(
    podcast_id: str,
    repository: PodcastRepository = Depends(get_podcast_repository),
) -> PodcastDetail:
    podcast = repository.get_by_id(podcast_id)
    if podcast is None:
        raise HTTPException(status_code=404, detail="podcast not found")
    return podcast


__all__ = [
    "PodcastGenerationDependencies",
    "Settings",
    "app",
    "build_podcast_generation_dependencies",
    "get_point_reader",
    "get_podcast_generation_dependencies",
    "get_podcast_repository",
    "get_settings",
    "run_podcast_generation",
    "run_podcast_generation_from_settings",
]
