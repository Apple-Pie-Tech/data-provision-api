import logging
import secrets
from collections.abc import Iterator
from dataclasses import dataclass
from functools import lru_cache

import boto3
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from app.config import Settings, get_settings
from app.podcast_clients import (  # pyright: ignore[reportMissingImports]
    DEFAULT_AUDIO_VOICE,
    DEFAULT_HOST_B_VOICE,
    BedrockScriptGenerator,
    FalCoverGenerator,
    PollyTTSClient,
    S3PodcastBlobStore,
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
from app.supabase_jwt import (  # pyright: ignore[reportMissingImports]
    SupabaseIdentity,
    SupabaseJwtError,
    SupabaseJwtUnavailableError,
    SupabaseJwtVerifier,
)
from app.universe import assemble_universe_graph  # pyright: ignore[reportMissingImports]
from app.url_signing import (  # pyright: ignore[reportMissingImports]
    S3PresignedUrlSigner,
    sign_podcast_urls,
    sign_universe_audio_urls,
)
from app.vector_store import (  # pyright: ignore[reportMissingImports]
    S3VectorsPointReader,
)


logger = logging.getLogger(__name__)


def _configured_provision_api_key(settings: Settings) -> str:
    return (settings.provision_api_key or "").strip()


def _configured_supabase_url(settings: Settings) -> str:
    return (settings.supabase_url or "").strip()


async def require_provision_api_key(
    settings: Settings = Depends(get_settings),
    x_api_key: str | None = Header(default=None),
) -> None:
    """Verify the ``x-api-key`` header against ``PROVISION_API_KEY``.

    Applied to POST /podcasts, which is the endpoint that costs money: each call
    makes a Bedrock call, several Polly calls and S3 writes. The read endpoints
    are deliberately left open, matching data-ingestion, where the same check
    guards POST /ingest and nothing else.

    When ``PROVISION_API_KEY`` is unset or blank the check is **skipped**, not
    failed, for the same reason as in data-ingestion: failing closed would leave
    a fresh clone with no ``.env`` unable to generate anything, and local
    development is the path this service has to keep working. ``create_app``
    logs a warning so an unprotected deployment is not silent.

    This is a deterrent, not authentication. The UI holds the key in an
    EXPO_PUBLIC_* variable, which is inlined into the public bundle, so it stops
    drive-by abuse of a discovered function URL rather than a determined caller.

    The 401 carries no detail about the expected key.
    """
    expected = _configured_provision_api_key(settings)
    if not expected:
        return

    provided = x_api_key or ""
    if not secrets.compare_digest(provided.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(status_code=401, detail="unauthorized")


@lru_cache(maxsize=1)
def _build_supabase_verifier(
    supabase_url: str,
    audience: str,
    hs256_secret: str | None,
    cache_seconds: int,
) -> SupabaseJwtVerifier:
    """One verifier per process, so its key cache survives between requests.

    Cached on the values rather than on Settings because Settings is not
    hashable. Under the Lambda Web Adapter the uvicorn process outlives a single
    invocation, so this is also the cross-invocation cache -- see
    app/supabase_jwt.py.
    """
    return SupabaseJwtVerifier(
        supabase_url=supabase_url,
        audience=audience,
        hs256_secret=hs256_secret,
        cache_seconds=cache_seconds,
    )


def get_supabase_verifier(
    settings: Settings = Depends(get_settings),
) -> SupabaseJwtVerifier | None:
    """None when SUPABASE_URL is unset, which skips verification entirely."""
    supabase_url = _configured_supabase_url(settings)
    if not supabase_url:
        return None

    return _build_supabase_verifier(
        supabase_url,
        settings.supabase_jwt_audience,
        (settings.supabase_jwt_secret or "").strip() or None,
        settings.supabase_jwks_cache_seconds,
    )


async def get_supabase_identity(
    verifier: SupabaseJwtVerifier | None = Depends(get_supabase_verifier),
    authorization: str | None = Header(default=None),
) -> SupabaseIdentity | None:
    """Verify the `Authorization: Bearer <supabase access token>` header.

    Applied to POST /podcasts, the endpoint that spends money: one Bedrock call,
    several Polly calls and S3 writes per request. The read endpoints stay open
    -- see README.md, "What is not protected", and the test that pins it.

    Returns None when ``SUPABASE_URL`` is unset, the same "unset means skip"
    convention as ``require_provision_api_key`` and for the same reason: local
    development has to keep working, and it lets this code deploy before the
    variable is set. ``create_app`` warns in that case.

    Once it *is* set there is no path back to unauthenticated. A JWKS endpoint
    that cannot be reached is a 503, never a 401 and never a 200: blaming the
    user for a Supabase outage is wrong, and treating a network error as a pass
    would be an authentication bypass.
    """
    if verifier is None:
        return None

    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(
            status_code=401,
            detail="unauthorized",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        return await verifier.verify(token.strip())
    except SupabaseJwtUnavailableError as exc:
        logger.exception("could not verify the access token: Supabase is unreachable")
        raise HTTPException(status_code=503, detail="auth_unavailable") from exc
    except SupabaseJwtError as exc:
        logger.warning("rejected an access token: %s", exc)
        raise HTTPException(
            status_code=401,
            detail="unauthorized",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


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

    if not _configured_provision_api_key(resolved_settings):
        logger.warning(
            "PROVISION_API_KEY is not set; POST /podcasts accepts unauthenticated "
            "requests, and each one spends Bedrock and Polly budget"
        )

    if not _configured_supabase_url(resolved_settings):
        # Worth its own line: with SUPABASE_URL unset the API key above is the
        # only gate, and it is public by construction -- the UI ships it in its
        # JavaScript bundle. This is the difference between a deterrent and
        # authentication.
        logger.warning(
            "SUPABASE_URL is not set; POST /podcasts accepts requests with no "
            "verified user"
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
            api_key=settings.fal_key,
            timeout_seconds=settings.podcast_timeout_seconds,
        )
    except Exception:
        cover_generator = None

    return PodcastGenerationDependencies(
        point_reader=point_reader,
        script_generator=BedrockScriptGenerator(
            region=settings.aws_region,
            model=settings.bedrock_script_model,
            timeout_seconds=settings.podcast_timeout_seconds,
            max_parts=settings.podcast_max_script_parts,
            max_tokens=settings.bedrock_script_max_tokens,
        ),
        tts_client=PollyTTSClient(
            region=settings.aws_region,
            timeout_seconds=settings.podcast_timeout_seconds,
            engine=settings.polly_engine,
            sample_rate=settings.polly_sample_rate,
            voice_models={
                DEFAULT_AUDIO_VOICE: settings.polly_voice_host_a,
                DEFAULT_HOST_B_VOICE: settings.polly_voice_host_b,
            },
        ),
        blob_store=S3PodcastBlobStore(
            bucket=settings.s3_podcast_bucket,
            region=settings.aws_region,
            timeout_seconds=settings.podcast_timeout_seconds,
        ),
        cover_generator=cover_generator,
        audio_merger=None,
    )


def record_bootstrap_failure(
    repository: PodcastRepository,
    podcast_id: str,
    exc: Exception,
) -> None:
    error = f"podcast generation could not start: {exc}"[:300]
    repository.mark_failed(podcast_id, error=error)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


def build_point_reader(settings: Settings) -> S3VectorsPointReader:
    return S3VectorsPointReader(
        boto3.client("s3vectors", region_name=settings.aws_region),
        settings.s3_vector_bucket or "",
        settings.s3_vector_index,
        page_size=settings.vector_list_batch_size,
    )


def get_point_reader(
    settings: Settings = Depends(get_settings),
) -> S3VectorsPointReader:
    """Build a reader for this request.

    No teardown: botocore holds a connection pool rather than an event-loop
    bound session, so there is nothing to await closed the way the async Qdrant
    client needed.
    """
    return build_point_reader(settings)


def get_podcast_repository() -> Iterator[PodcastRepository]:
    repository = PodcastRepository()
    try:
        repository.init_db()
        yield repository
    finally:
        repository.close()


def get_podcast_generation_dependencies() -> PodcastGenerationDependencies | None:
    return None


def get_url_signer(settings: Settings = Depends(get_settings)) -> S3PresignedUrlSigner:
    return S3PresignedUrlSigner(
        region=settings.aws_region,
        ttl_minutes=settings.presigned_url_ttl_minutes,
    )


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
    try:
        repository = PodcastRepository()
        repository.init_db()
        point_reader = build_point_reader(settings)
        generation = build_podcast_generation_dependencies(
            settings=settings,
            point_reader=point_reader,
        )
        await run_podcast_generation(podcast_id, repository, generation, settings)
    except Exception as exc:
        # Nothing downstream of this background task can report an error, so a
        # failure while bootstrapping must be written to the row itself —
        # otherwise it stays pending forever.
        if repository is None:
            raise
        record_bootstrap_failure(repository, podcast_id, exc)
    finally:
        if repository is not None:
            repository.close()


@app.get("/universe", response_model=UniverseResponse)
async def get_universe(
    point_reader: S3VectorsPointReader = Depends(get_point_reader),
    signer: S3PresignedUrlSigner = Depends(get_url_signer),
) -> UniverseResponse:
    try:
        points = await point_reader.read_points()
    except Exception as exc:
        # The 503 body is deliberately opaque to the caller, so without this the
        # cause is lost entirely: an empty or failing /universe was bug E7 and
        # the only signal was the status code. `from exc` preserves the chain
        # for a local traceback but writes nothing to the log in Lambda.
        logger.exception("reading the vector store for /universe failed")
        raise HTTPException(status_code=503, detail="vector store unavailable") from exc
    return sign_universe_audio_urls(assemble_universe_graph(points), signer)


@app.post(
    "/podcasts",
    response_model=PodcastDetail,
    status_code=202,
    # Both gates listed here, so an unauthenticated caller is refused before
    # the body is parsed and long before any billable work starts.
    dependencies=[Depends(require_provision_api_key), Depends(get_supabase_identity)],
)
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
    signer: S3PresignedUrlSigner = Depends(get_url_signer),
) -> list[PodcastListItem]:
    return [sign_podcast_urls(podcast, signer) for podcast in repository.list()]


@app.get("/podcasts/{podcast_id}", response_model=PodcastDetail)
async def get_podcast(
    podcast_id: str,
    repository: PodcastRepository = Depends(get_podcast_repository),
    signer: S3PresignedUrlSigner = Depends(get_url_signer),
) -> PodcastDetail:
    podcast = repository.get_by_id(podcast_id)
    if podcast is None:
        raise HTTPException(status_code=404, detail="podcast not found")
    return sign_podcast_urls(podcast, signer)


__all__ = [
    "PodcastGenerationDependencies",
    "Settings",
    "app",
    "build_podcast_generation_dependencies",
    "get_supabase_identity",
    "get_supabase_verifier",
    "get_url_signer",
    "get_point_reader",
    "get_podcast_generation_dependencies",
    "get_podcast_repository",
    "get_settings",
    "record_bootstrap_failure",
    "run_podcast_generation",
    "run_podcast_generation_from_settings",
]
