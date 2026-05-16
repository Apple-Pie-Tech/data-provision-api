# Data Provision API: Podcast Generation Additions

## TL;DR
> **Summary**: Add a hackathon-simple podcast generation flow: create a background job for a selected Qdrant topic, generate a two-host script with OpenAI, synthesize audio with slng.ai, merge/upload assets to Azure Blob Storage, store status/script/URLs in Azure PostgreSQL, and expose list/detail GET routes for the UI.
> **Deliverables**:
> - Azure PostgreSQL setup instructions via `az` CLI
> - Podcast persistence layer and DB table bootstrap
> - Topic-filtered Qdrant chunk loading
> - OpenAI/slng.ai/fal.ai/Azure Blob integration wrappers
> - `POST /podcasts`, `GET /podcasts`, and `GET /podcasts/{id}`
> - Focused pytest coverage with external-service fakes
> **Effort**: Medium
> **Parallel**: YES - 4 waves
> **Critical Path**: DB/config → Qdrant topic loader → generation services → background route wiring → tests/QA

## Context

### Original Request
- Add a `POST` route that generates a podcast based on a selected topic/label, using all chunks from that topic.
- Add a database to store the generated podcast and its script, hosted on Azure using the Azure CLI.
- Add a `GET` route to retrieve generated podcasts from the database for the UI.
- Generation flow: Qdrant chunks by label → OpenAI storyline/script → slng.ai audio per text chunk/voice → merge audio → fal.ai cover image → store everything.
- No controls for voice, image generation, or storyline.
- Keep it simple and hackathon-level: it does not need to be perfect, but it must work.

### Interview Summary
- Asset storage: use Azure Blob Storage, not database blobs or local files.
- Existing storage account: `applepieingestaudio`.
- Azure CLI verified: account `applepieingestaudio`, resource group `applepie-data-ingestion-rg`, location `westeurope`, kind `StorageV2`, SKU `Standard_LRS`.
- `POST /podcasts` should create a background job, not block until full generation completes.
- Read API should use list plus detail: `GET /podcasts` and `GET /podcasts/{id}`.
- Create this as a separate additive plan because `.sisyphus/plans/data-provision-vector-model.md` is currently being implemented.

### Gap Review Addressed
- Metis delegation was blocked by the plan-agent runtime; gap review was performed directly in this plan.
- Oracle architecture review accepted FastAPI `BackgroundTasks` for demo scope only with explicit job status, strict limits, timeouts, and documented non-durability.
- Guardrail: no Celery/RQ/Redis, no auth, no user controls, no complex orchestration.
- Guardrail: podcast identity is a UUID row id; `label` is input metadata only.
- Guardrail: job state machine is `pending -> running -> completed | failed`.
- Guardrail: cover generation is best-effort; audio generation is the core product.

## Work Objectives

### Core Objective
Enable the UI to request a podcast for a topic and later fetch generated podcast records with script, audio URL, cover URL, status, and errors.

### Deliverables
- `app/podcast_schemas.py` for request/response/script models.
- `app/db.py` for minimal Postgres connection, table creation, and CRUD helpers.
- `app/podcast_repository.py` for podcast row operations.
- `app/podcast_generation.py` for orchestration.
- `app/podcast_clients.py` for OpenAI, slng.ai, fal.ai, and Azure Blob wrappers.
- `app/vector_store.py` extension for label-filtered chunk loading.
- `app/main.py` route wiring for `POST /podcasts`, `GET /podcasts`, and `GET /podcasts/{id}`.
- `.env.example` documenting required environment variables.
- Tests for DB/repository behavior, route behavior, and generation happy/failure paths.

### Definition of Done (verifiable conditions with commands)
- `pytest` passes.
- `GET /health` still returns `{ "status": "ok" }`.
- `POST /podcasts` with `{ "label": "some-topic" }` creates a podcast row and returns `202` with `id`, `label`, and `status`.
- Background generation updates the row to `completed` with `script`, `audio_url`, and `cover_url` when faked services succeed.
- Background generation updates the row to `failed` with a usable `error` when Qdrant chunks are empty or a required service fails.
- `GET /podcasts` returns list-card data.
- `GET /podcasts/{id}` returns detail data including script JSON.
- No endpoint exposes OpenAI, SLNG, fal.ai, Azure, or database secrets.

### Must Have
- Azure PostgreSQL Flexible Server setup command documented in `.env.example` or a short setup section in the plan evidence.
- Runtime dependencies added: `psycopg[binary]`, `openai`, `fal-client`, `azure-storage-blob`, `pydub`, and runtime `httpx`.
- Use `pydub` for audio merge and document local/runtime `ffmpeg` requirement. Prefer faked merge in tests.
- DB table columns: `id`, `label`, `status`, `script_json`, `audio_url`, `cover_url`, `error`, `created_at`, `updated_at`, `started_at`, `completed_at`.
- Config fields: `DATABASE_URL`, `OPENAI_API_KEY`, `SLNG_API_KEY`, `FAL_KEY`, `AZURE_STORAGE_ACCOUNT`, `AZURE_STORAGE_CONTAINER`, `AZURE_STORAGE_CONNECTION_STRING`, max chunks, max script parts, timeout seconds.
- Default blob container name: `podcasts`.
- Blob path scheme: `podcasts/{podcast_id}/podcast.mp3` and `podcasts/{podcast_id}/cover.png`.
- Hard generation limits: default max 40 chunks, max 12 script parts/clips, max 120 seconds per whole job unless executor chooses lower.

### Must NOT Have
- No production queue, Celery, RQ, Redis, Durable Functions, or Kubernetes jobs.
- No auth or tenant model.
- No voice selection UI/API.
- No image prompt controls.
- No storyline controls.
- No migration framework unless implementation proves raw bootstrap impossible.
- No changes that reintroduce removed universe fields `title`, `durationSeconds`, or `category`.
- No modification of Qdrant data.

## Verification Strategy
> ZERO HUMAN INTERVENTION - all verification is agent-executed.
- Test decision: tests-after with pytest, matching current repo style.
- QA policy: every implementation task includes faked happy-path and failure-path scenarios.
- Evidence: `.sisyphus/evidence/task-{N}-{slug}.txt`
- External APIs are tested through fake clients; do not call paid external services in automated tests.
- Azure CLI/storage checks are documented as evidence, not required for unit tests.

## Execution Strategy

### Parallel Execution Waves
> Target: 5-8 tasks per wave. This plan is intentionally smaller because the repo is small and dependencies are sequential.

Wave 1: Tasks 1-3 foundation/config/persistence/vector filtering
Wave 2: Tasks 4-6 external clients, generation orchestrator, blob/audio handling
Wave 3: Tasks 7-8 route wiring and Azure setup docs
Wave 4: Final verification

### Dependency Matrix (full, all tasks)
- Task 1 blocks Tasks 2, 4, 5, 7, 8.
- Task 2 blocks Tasks 5 and 7.
- Task 3 blocks Task 5.
- Task 4 blocks Task 5.
- Task 5 blocks Task 7.
- Task 6 blocks Task 5 and Task 7.
- Task 7 blocks final verification.
- Task 8 can run after Task 1 and before final verification.

### Agent Dispatch Summary (wave → task count → categories)
- Wave 1 → 3 tasks → `quick`, `quick`, `quick`
- Wave 2 → 3 tasks → `quick`, `unspecified-high`, `quick`
- Wave 3 → 2 tasks → `quick`, `writing`
- Final → 4 review tasks → `oracle`, `unspecified-high`, `unspecified-high`, `deep`

## TODOs
> Implementation + Test = ONE task. Never separate.
> EVERY task MUST have: Agent Profile + Parallelization + QA Scenarios.

- [x] 1. Add podcast config, schemas, and dependencies

  **What to do**: Update `pyproject.toml` dependencies for runtime HTTP/API/storage/DB work. Add podcast request/response/script models in new file `app/podcast_schemas.py`. Extend `app/config.py` with podcast env settings and safe defaults: API keys, database URL, Azure storage account/container, max chunks, max script parts, and timeout seconds. Add `.env.example` with placeholders and no secrets.
  **Must NOT do**: Do not add auth, queue dependencies, frontend code, or real secrets.

  **Recommended Agent Profile**:
  - Category: `quick` - focused config/schema/dependency update
  - Skills: [] - no special skill required
  - Omitted: [`supabase-postgres-best-practices`] - database is Azure PostgreSQL but the task is app config, not query optimization

  **Parallelization**: Can Parallel: YES | Wave 1 | Blocks: 2, 4, 5, 7, 8 | Blocked By: none

  **References**:
  - Pattern: `app/config.py:6-16` - current Pydantic settings and `.env` loading style
  - Pattern: `app/schemas.py:4-25` - current small Pydantic model style
  - Pattern: `pyproject.toml` - current setuptools dependency location
  - External: https://developers.openai.com/api/docs/guides/structured-outputs - script generation should use structured outputs
  - External: https://docs.slng.ai/examples/tts-http - slng.ai HTTP TTS path
  - External: https://docs.fal.ai/model-apis/clients/python/ - fal Python client usage

  **Acceptance Criteria**:
  - [ ] `pytest` still imports the app successfully.
  - [ ] Config exposes all required env vars with safe optional defaults where possible.
  - [ ] `.env.example` documents `DATABASE_URL`, `OPENAI_API_KEY`, `SLNG_API_KEY`, `FAL_KEY`, `AZURE_STORAGE_ACCOUNT=applepieingestaudio`, `AZURE_STORAGE_CONTAINER=podcasts`, and `AZURE_STORAGE_CONNECTION_STRING`.
  - [ ] Podcast response schemas serialize without leaking secret/config fields.

  **QA Scenarios**:
  ```
  Scenario: Podcast schemas serialize
    Tool: Bash
    Steps: run `pytest tests/test_podcast_schemas.py`
    Expected: request/list/detail/script models serialize with id, label, status, script/audio/cover fields
    Evidence: .sisyphus/evidence/task-1-podcast-config-schemas.txt

  Scenario: Missing optional env does not crash import
    Tool: Bash
    Steps: run `pytest tests/test_main.py`
    Expected: health route still passes without real external API keys
    Evidence: .sisyphus/evidence/task-1-podcast-config-schemas-error.txt
  ```

  **Commit**: NO | Message: `feat(podcasts): add generation config and schemas` | Files: `pyproject.toml`, `app/config.py`, `app/podcast_schemas.py`, `.env.example`, `tests/test_podcast_schemas.py`

- [x] 2. Add minimal PostgreSQL podcast persistence

  **What to do**: Add `app/db.py` and `app/podcast_repository.py` using `psycopg` with simple SQL, not an ORM. Implement `init_db()` that creates a `podcasts` table if missing. Implement create, mark running, mark completed, mark failed, list, and get-by-id helpers. Use UUID string ids generated by the app. Store `script_json` as JSON/JSONB, `status` as text, and timestamps as timezone-aware values.
  **Must NOT do**: Do not add Alembic, SQLAlchemy, migration frameworks, or complex repository abstractions.

  **Recommended Agent Profile**:
  - Category: `quick` - small persistence layer with tests
  - Skills: [] - no special skill required
  - Omitted: [`terraform-engineer`] - no IaC; Azure setup is CLI documentation only

  **Parallelization**: Can Parallel: YES | Wave 1 | Blocks: 5, 7 | Blocked By: 1

  **References**:
  - Pattern: `tests/test_vector_store.py:38-148` - fake dependency testing style
  - Pattern: `app/config.py:6-16` - read `DATABASE_URL` from settings
  - Oracle guardrail: explicit `pending -> running -> completed | failed` state machine

  **Acceptance Criteria**:
  - [ ] Repository can create a pending podcast row with UUID id and label.
  - [ ] Repository can transition status to running, completed, and failed.
  - [ ] List query omits large script details or keeps response lightweight according to schema.
  - [ ] Detail query returns script JSON and error fields.
  - [ ] Tests use a fake repository or temporary DB strategy; they must not require real Azure PostgreSQL.

  **QA Scenarios**:
  ```
  Scenario: Podcast row lifecycle
    Tool: Bash
    Steps: run `pytest tests/test_podcast_repository.py`
    Expected: pending row transitions to running then completed with script/audio/cover URLs
    Evidence: .sisyphus/evidence/task-2-podcast-db.txt

  Scenario: Failed job stores error
    Tool: Bash
    Steps: run repository test that marks a row failed
    Expected: status is failed and error string is retrievable via detail helper
    Evidence: .sisyphus/evidence/task-2-podcast-db-error.txt
  ```

  **Commit**: NO | Message: `feat(podcasts): persist generated podcast jobs` | Files: `app/db.py`, `app/podcast_repository.py`, `tests/test_podcast_repository.py`

- [x] 3. Add Qdrant topic chunk loader

  **What to do**: Extend `app/vector_store.py` with a method that returns chunks for one `label`. Use Qdrant payload filtering if the client supports it in current dependency version; otherwise read and filter in app code with the configured max chunk limit. Return chunk text plus point id/audio_url/source metadata if available. Preserve existing `read_points()` behavior for universe graph work.
  **Must NOT do**: Do not write to Qdrant. Do not change existing universe graph semantics.

  **Recommended Agent Profile**:
  - Category: `quick` - focused adapter extension
  - Skills: [] - no special skill required
  - Omitted: [`systematic-debugging`] - no current bug investigation

  **Parallelization**: Can Parallel: YES | Wave 1 | Blocks: 5 | Blocked By: none

  **References**:
  - Pattern: `app/vector_store.py:7-95` - existing async Qdrant scroll reader and normalization
  - Pattern: `.sisyphus/plans/data-provision-vector-model.md:187-224` - original Qdrant reader constraints

  **Acceptance Criteria**:
  - [ ] `read_chunks_by_label(label)` or equivalent returns only matching label chunks.
  - [ ] Empty/missing label returns an empty list, not an exception.
  - [ ] Max chunk limit is enforced.
  - [ ] Existing vector-store tests still pass.

  **QA Scenarios**:
  ```
  Scenario: Load chunks for selected topic
    Tool: Bash
    Steps: run `pytest tests/test_vector_store.py`
    Expected: fake Qdrant data with two labels returns only requested label chunks
    Evidence: .sisyphus/evidence/task-3-qdrant-topic-loader.txt

  Scenario: Unknown topic has no chunks
    Tool: Bash
    Steps: run vector-store test for a missing label
    Expected: empty list and no exception
    Evidence: .sisyphus/evidence/task-3-qdrant-topic-loader-error.txt
  ```

  **Commit**: NO | Message: `feat(podcasts): load qdrant chunks by topic` | Files: `app/vector_store.py`, `tests/test_vector_store.py`

- [x] 4. Add external client wrappers for OpenAI, slng.ai, fal.ai, and Azure Blob

  **What to do**: Add `app/podcast_clients.py` with small wrapper classes/functions: OpenAI structured script generation, slng.ai HTTP TTS for text chunks, fal.ai cover generation, and Azure Blob upload. Each wrapper must accept injected API clients or transport where practical so tests can fake them. Set timeouts. Use deterministic default voices internally, such as `host_a` and `host_b`; do not expose voice controls in API.
  **Must NOT do**: Do not call external services during tests. Do not expose API keys in responses or logs.

  **Recommended Agent Profile**:
  - Category: `quick` - adapter wrappers with fakeable tests
  - Skills: [] - no special skill required
  - Omitted: [`fine-tuning-expert`] - no model training involved

  **Parallelization**: Can Parallel: YES | Wave 2 | Blocks: 5 | Blocked By: 1

  **References**:
  - External: https://developers.openai.com/api/docs/guides/structured-outputs - use schema-validated script output
  - External: https://docs.slng.ai/examples/tts-http - use HTTP TTS, not WebSocket streaming
  - External: https://docs.fal.ai/model-apis/clients/python/ - use `fal-client` server-side with `FAL_KEY`
  - Azure storage account: `applepieingestaudio`, resource group `applepie-data-ingestion-rg`

  **Acceptance Criteria**:
  - [ ] OpenAI wrapper returns a validated script object with bounded number of parts.
  - [ ] slng.ai wrapper returns audio bytes for each script part or raises a controlled error.
  - [ ] fal wrapper returns cover image bytes or URL that can be uploaded/stored.
  - [ ] Blob wrapper uploads audio and cover to `podcasts/{id}/...` paths and returns usable URLs.
  - [ ] All wrappers have timeout/error tests using fakes/mocks.

  **QA Scenarios**:
  ```
  Scenario: Fake clients produce script/audio/cover/blob URLs
    Tool: Bash
    Steps: run `pytest tests/test_podcast_clients.py`
    Expected: fake OpenAI/slng/fal/blob clients return expected structured outputs and URLs
    Evidence: .sisyphus/evidence/task-4-podcast-clients.txt

  Scenario: TTS failure is controlled
    Tool: Bash
    Steps: fake slng.ai client raises timeout/error
    Expected: wrapper raises a typed/controlled exception without logging secrets
    Evidence: .sisyphus/evidence/task-4-podcast-clients-error.txt
  ```

  **Commit**: NO | Message: `feat(podcasts): add generation service clients` | Files: `app/podcast_clients.py`, `tests/test_podcast_clients.py`

- [x] 5. Add podcast generation orchestrator

  **What to do**: Add `app/podcast_generation.py` that performs the background job. Steps: mark row running, load chunks by label, fail if no chunks, trim to configured max chunks, call OpenAI for script, trim/validate to max parts, call slng.ai per script part, merge audio clips, call fal.ai for cover with fallback placeholder if cover fails, upload final audio and cover to Blob, mark completed. On required failure, mark failed with concise error. Keep this function callable directly in tests.
  **Must NOT do**: Do not implement durable retries, scheduling, cancellation, or queue workers.

  **Recommended Agent Profile**:
  - Category: `unspecified-high` - central orchestration with multiple dependencies and error paths
  - Skills: [] - no special skill required
  - Omitted: [`backend-development:workflow-orchestration-patterns`] - Temporal/durable workflows are out of scope

  **Parallelization**: Can Parallel: NO | Wave 2 | Blocks: 7 | Blocked By: 2, 3, 4, 6

  **References**:
  - Pattern: `app/universe.py:9-53` - pure transformation style for business logic
  - Pattern: `tests/test_vector_store.py:38-148` - fake dependency style
  - Oracle guardrail: background work is non-durable; DB status is mandatory for debuggability

  **Acceptance Criteria**:
  - [ ] Happy-path generation marks row completed with script, audio URL, and cover URL.
  - [ ] Empty Qdrant chunks marks row failed with user-usable error.
  - [ ] Required service failure marks row failed.
  - [ ] Cover failure does not fail the whole job if audio generation/upload succeeds; use placeholder/fallback cover behavior.
  - [ ] Partial failures never leave a row stuck in running in tests.

  **QA Scenarios**:
  ```
  Scenario: Complete podcast generation with fakes
    Tool: Bash
    Steps: run `pytest tests/test_podcast_generation.py::test_generate_podcast_happy_path`
    Expected: status completed, script saved, audio_url and cover_url saved
    Evidence: .sisyphus/evidence/task-5-generation-orchestrator.txt

  Scenario: No chunks fails clearly
    Tool: Bash
    Steps: run generation test with fake Qdrant returning no chunks
    Expected: status failed and error mentions no chunks for label
    Evidence: .sisyphus/evidence/task-5-generation-orchestrator-error.txt
  ```

  **Commit**: NO | Message: `feat(podcasts): orchestrate background generation` | Files: `app/podcast_generation.py`, `tests/test_podcast_generation.py`

- [x] 6. Add simple audio merge helper

  **What to do**: Add `app/audio.py` with a `pydub`-based helper that merges generated MP3 audio clips into one MP3 byte stream. Document `ffmpeg` in `.env.example`/setup notes. Tests should monkeypatch the merge function rather than require real audio decoding.
  **Must NOT do**: Do not build timeline editing, normalization, silence controls, or waveform analysis.

  **Recommended Agent Profile**:
  - Category: `quick` - small helper with test seam
  - Skills: [] - no special skill required
  - Omitted: [`frontend-design`] - no UI work

  **Parallelization**: Can Parallel: YES | Wave 2 | Blocks: 5, 7 | Blocked By: 1

  **References**:
  - User requirement: merge all generated audio into one podcast audio
  - Oracle guardrail: keep audio as core product; cover is best-effort

  **Acceptance Criteria**:
  - [ ] Merge helper has a deterministic interface: `list[bytes] -> bytes` or file path.
  - [ ] Generation orchestrator can inject/monkeypatch merge helper in tests.
  - [ ] Setup notes document `ffmpeg` because `pydub` is used.

  **QA Scenarios**:
  ```
  Scenario: Merge helper is called by generation
    Tool: Bash
    Steps: run generation test with monkeypatched merge helper
    Expected: merged bytes are uploaded as final podcast audio
    Evidence: .sisyphus/evidence/task-6-audio-merge.txt

  Scenario: Merge failure marks job failed
    Tool: Bash
    Steps: fake merge helper raises an exception
    Expected: podcast status becomes failed with merge-related error
    Evidence: .sisyphus/evidence/task-6-audio-merge-error.txt
  ```

  **Commit**: NO | Message: `feat(podcasts): merge generated audio clips` | Files: `app/audio.py`, `app/podcast_generation.py`, `tests/test_podcast_generation.py`

- [x] 7. Wire podcast routes and background task

  **What to do**: Update `app/main.py` or add a small router module to expose `POST /podcasts`, `GET /podcasts`, and `GET /podcasts/{id}`. `POST /podcasts` validates label, creates pending DB row, schedules `BackgroundTasks.add_task(generate_podcast, podcast_id)`, and returns `202`. GET list returns newest podcasts first. GET detail returns 404 for unknown id. Add dependency seams for fake repository/generator in tests; keep the pattern as simple as possible.
  **Must NOT do**: Do not wait for generation in the POST request. Do not add polling-specific routes beyond list/detail. Do not add auth.

  **Recommended Agent Profile**:
  - Category: `quick` - route wiring and endpoint tests
  - Skills: [] - no special skill required
  - Omitted: [`agent-browser`] - API-only, no browser testing required

  **Parallelization**: Can Parallel: NO | Wave 3 | Blocks: Final Verification | Blocked By: 2, 5, 6

  **References**:
  - Pattern: `app/main.py:5-10` - current FastAPI app style
  - Pattern: `tests/test_main.py:6-12` - current TestClient endpoint test style
  - Existing missing work: `.sisyphus/plans/data-provision-vector-model.md:266-303` - `/universe` endpoint still pending; avoid conflicts if editing `app/main.py`

  **Acceptance Criteria**:
  - [ ] `POST /podcasts` returns 202 and created podcast id/status.
  - [ ] `GET /podcasts` returns list response with created/completed/failed podcasts.
  - [ ] `GET /podcasts/{id}` returns full detail including script JSON.
  - [ ] Unknown detail id returns 404.
  - [ ] Invalid/blank label returns 422 or 400.
  - [ ] Existing `GET /health` test still passes.

  **QA Scenarios**:
  ```
  Scenario: Create podcast job
    Tool: Bash
    Steps: run `pytest tests/test_podcast_routes.py::test_post_podcasts_creates_background_job`
    Expected: HTTP 202, pending/running status, fake background task scheduled
    Evidence: .sisyphus/evidence/task-7-podcast-routes.txt

  Scenario: Unknown podcast detail
    Tool: Bash
    Steps: run route test requesting `/podcasts/not-found`
    Expected: HTTP 404 with safe error body
    Evidence: .sisyphus/evidence/task-7-podcast-routes-error.txt
  ```

  **Commit**: NO | Message: `feat(podcasts): expose podcast generation routes` | Files: `app/main.py`, `app/podcast_routes.py`, `tests/test_podcast_routes.py`

- [x] 8. Document Azure setup and runtime caveats

  **What to do**: Add a concise setup note in `.env.example` comments or `README.md` if one exists; if no README exists, keep setup in `.env.example` comments and evidence. Include Azure CLI commands to create PostgreSQL Flexible Server, add firewall/public access suitable for hackathon, create/select Blob container in `applepieingestaudio`, and set app env vars. State that FastAPI `BackgroundTasks` are not durable across process restarts.
  **Must NOT do**: Do not create or modify Azure resources unless user explicitly asks execution agent to do so during implementation. Do not store real passwords in files.

  **Recommended Agent Profile**:
  - Category: `writing` - setup documentation and command clarity
  - Skills: [] - no special skill required
  - Omitted: [`terraform-engineer`] - no Terraform desired for hackathon

  **Parallelization**: Can Parallel: YES | Wave 3 | Blocks: Final Verification | Blocked By: 1

  **References**:
  - Verified account: `applepieingestaudio`, resource group `applepie-data-ingestion-rg`, location `westeurope`
  - External: https://learn.microsoft.com/en-us/azure/postgresql/configure-maintain/quickstart-create-server - Azure PostgreSQL quickstart
  - External: https://learn.microsoft.com/en-us/cli/azure/postgres/flexible-server?view=azure-cli-latest - CLI reference

  **Acceptance Criteria**:
  - [ ] Setup notes include exact `az postgres flexible-server create` command template with placeholders.
  - [ ] Setup notes include Blob container command template for storage account `applepieingestaudio`.
  - [ ] Setup notes include `DATABASE_URL` with `sslmode=require`.
  - [ ] Setup notes explicitly warn that background jobs are not durable across server restarts.

  **QA Scenarios**:
  ```
  Scenario: Setup docs contain required env vars
    Tool: Bash
    Steps: run a simple test or grep-style assertion in `pytest tests/test_env_example.py`
    Expected: `.env.example` mentions all required vars and no real secrets
    Evidence: .sisyphus/evidence/task-8-azure-setup-docs.txt

  Scenario: Azure commands are templates, not secrets
    Tool: Bash
    Steps: test `.env.example` for placeholder password/API key markers
    Expected: no committed real key/password patterns
    Evidence: .sisyphus/evidence/task-8-azure-setup-docs-error.txt
  ```

  **Commit**: NO | Message: `docs(podcasts): document azure runtime setup` | Files: `.env.example`, `tests/test_env_example.py`

## Final Verification Wave (MANDATORY — after ALL implementation tasks)
> 4 review agents run in PARALLEL. ALL must APPROVE. Present consolidated results to user and get explicit "okay" before completing.
> **Do NOT auto-proceed after verification. Wait for user's explicit approval before marking work complete.**
> **Never mark F1-F4 as checked before getting user's okay.** Rejection or user feedback -> fix -> re-run -> present again -> wait for okay.
- [ ] F1. Plan Compliance Audit — oracle
- [ ] F2. Code Quality Review — unspecified-high
- [ ] F3. Real Manual QA — unspecified-high
- [ ] F4. Scope Fidelity Check — deep

## Commit Strategy
- Keep this podcast addition as one commit after tests pass unless the user asks for smaller commits.
- Suggested message: `feat(podcasts): generate podcasts from qdrant topics`
- Do not commit until user explicitly asks for git operations.

## Success Criteria
- A UI can call `POST /podcasts` with a topic label and receive a podcast job id.
- Background generation can complete using faked tests and real env-configured services in manual execution.
- Completed podcast rows include script JSON, audio URL, and cover URL.
- Failed podcast rows include a clear error and never remain stuck in `running` in tested failure paths.
- Audio and cover assets are stored in Azure Blob Storage account `applepieingestaudio`.
- Podcast metadata and script are stored in Azure PostgreSQL.
- `GET /podcasts` and `GET /podcasts/{id}` provide UI-ready data.
