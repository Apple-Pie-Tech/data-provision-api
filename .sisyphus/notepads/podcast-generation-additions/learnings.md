## 2026-05-16 end-to-end integration snapshot

- UI (`applepie-ui`) is still local-first. `src/features/recording/recording-state.tsx` only manages recorder permissions/lifecycle and keeps the last recording in memory; `src/features/universe/universe-screen.tsx` renders the record/generate UI, but the repo has no backend fetch/post calls in `src/` yet.
- Ingestion (`data-ingestion`) is the only fully wired upstream entrypoint. `POST /ingest` in `app/main.py` accepts multipart `metadata` + optional `text`/`audio`, then `app/ingestion.py` transcribes text/audio, chunks, embeds, and upserts to Qdrant through `app/vector_store.py`.
- The ingestion Qdrant writes are real and deterministic: `app/vector_store.py` stores payloads with `input_id`, `user_id`, `timestamp`, `chunk_index`, `text`, `source`, and `audio_url`, and uses UUIDv5 point IDs derived from `<input_id>:<chunk_index>`.
- Story labeling (`story-labeling-api`) is also real but still externally triggered. `POST /cluster-labels` in `app/main.py` loads story points from Qdrant, clusters them in `app/service.py`, labels each cluster through Azure OpenAI in `app/labeling.py`, then writes clustering payloads and centroid points back via `app/vector_store.py`.
- Data provision (`data-provision-api`) is wired for read/generate, not for upstream orchestration. `GET /universe` reads points from Qdrant, and `POST /podcasts` creates a DB row then background-generates audio/cover assets from points that match the podcast label.
- The main remaining gap is orchestration: there is no code path connecting UI recording → ingestion upload → labeling trigger → provisioning fetch/generate. That flow still needs an explicit API client or job coordinator.
- Config drift still exists in the checked-in env files: `data-ingestion/.env` still shows the legacy Gradium transcription path, while `app/transcription.py` normalizes it to `/post/speech/asr`; `story-labeling-api/.env.example` and `data-provision-api/.env.example` are aligned with their current Qdrant/database expectations.

## 2026-05-16 fix plan

### Validation loop — applies to every phase
- Do not treat implementation as done when code compiles. Each phase must end with a working observable check against either local services or the deployed Koyeb apps.
- Prefer local validation while building a single hop, then use deployed validation to confirm the real environment still behaves the same.
- Reuse the repo's existing evidence surfaces where possible:
  - `data-ingestion`: `/health`, `POST /ingest`, `scripts/smoke_text_ingest.sh`, and `tests/test_external_smoke.py`
  - `story-labeling-api`: `/health` and `POST /cluster-labels`
  - `data-provision-api`: `/health`, `GET /universe`, `POST /podcasts`, and the route tests in `tests/test_podcast_routes.py`
  - `applepie-ui`: `expo start` / `expo start --web`, plus manual UI interaction until UI-facing automation exists
- The rule for the whole effort: no phase closes until there is a repeatable check that would fail again if the phase regressed.

### Current blockers to reliable evaluation
- `applepie-ui/src/features/recording/recording-state.tsx` only finalizes local recording state; it does not post to `data-ingestion`, so UI-side success currently means only "recording finished locally".
- `applepie-ui/src/features/universe/universe-screen.tsx` and `src/features/universe/universe-data.ts` still render static data, so there is no live proof yet that provisioning data is reaching the UI.
- `story-labeling-api/app/main.py` exposes `POST /cluster-labels`, but it is still a manual trigger; the current flow cannot prove automatic ingest → labeling orchestration.
- `data-provision-api/app/main.py` and `app/podcast_repository.py` already support podcast status lifecycle, but the UI is not wired to `/podcasts` or `/podcasts/{id}`, so generation status is still backend-only.
- `data-ingestion/tests/test_external_smoke.py` is currently the only checked-in external smoke path. The other repos still need equivalent end-to-end smoke coverage.

### Preferred evaluation ladder
- Phase 2 strongest proof: UI action + local/deployed `POST /ingest` response + Qdrant-visible indexed payload.
- Phase 3 strongest proof: successful labeling trigger + observable clustering payloads / centroid points written back.
- Phase 4 strongest proof: UI reads live `/universe` data and can create then observe `/podcasts` state transitions.
- Phase 5 strongest proof: one sample can be traced through all four services with the same input identifier / label context.

### Phase 1 — Freeze the integration contract
- Define the shared request/response shapes for:
  - UI → ingestion upload
  - ingestion → labeling trigger
  - UI → provisioning fetch/generate
- Decide the single source of truth for service base URLs and env vars.
- Outcome: every repo agrees on the same endpoints, payload fields, and error contract.
- Exit criteria:
  - one written contract matrix for the four hops exists in the state note
  - one validation matrix exists listing the local and deployed command/check for each hop
- Parallelizable: yes, the contract docs and env cleanup can be done in parallel once the shape is agreed.

#### Phase 1 progress — contract matrix

| Hop | Current caller state | Contract |
|---|---|---|
| UI → data-ingestion | No UI networking exists yet in `applepie-ui`; `src/app/index.tsx`, `src/app/record.tsx`, `src/features/universe/universe-screen.tsx`, and `src/features/recording/recording-state.tsx` are local-only today. | Server contract already exists at `data-ingestion/app/main.py`: `POST /ingest` with `multipart/form-data` containing `metadata` (JSON string for `input_id`, `user_id`, `timestamp`), optional `text`, optional `audio`. Response model from `data-ingestion/app/schemas.py`: `{ input_id, status, chunks, audio_url? }`. Key errors: invalid metadata, missing text/audio, oversized audio, transcription/embedding/audio-storage/vector-store failures. `data-ingestion/tests/test_ingest_contract.py` proves text wins over audio. |
| data-ingestion → story-labeling trigger | No automatic trigger exists in `data-ingestion`; this hop is absent in current code. | Labeling API contract already exists at `story-labeling-api/app/main.py`: `POST /cluster-labels` with no request body. Response model from `story-labeling-api/app/schemas.py`: `{ status, points_read, points_clustered, clusters_found, noise_points, points_updated }`. Key errors: `400` clustering failure, `502 labeling_unavailable`, `503 vector_store_unavailable`, `500 internal_server_error`. `story-labeling-api/PLAN.md` still describes this as a manual trigger. |
| UI → data-provision-api read | No UI networking exists yet; UI still renders static universe data from `applepie-ui/src/features/universe/universe-data.ts`. | Read contract exists at `data-provision-api/app/main.py`: `GET /universe` with no body. Response model from `data-provision-api/app/schemas.py`: `{ points: UniversePoint[], edges: UniverseEdge[] }`, where points include `{ id, label, audio_url?, is_synthetic, is_central }`. Key error: `503 vector store unavailable`. Proven by `data-provision-api/tests/test_main.py`. |
| UI → data-provision-api generate/status | No UI caller exists yet. | Generate contract exists at `data-provision-api/app/main.py`: `POST /podcasts` with JSON body `{ label }`, returns `202` and `PodcastDetail` from `data-provision-api/app/podcast_schemas.py`: `{ id, label, status, audio_url?, cover_url?, script?, error? }`. Status contracts also exist: `GET /podcasts` returns list items `{ id, label, status, audio_url?, cover_url? }`; `GET /podcasts/{podcast_id}` returns `PodcastDetail`; `404` when missing. Proven by `data-provision-api/tests/test_podcast_routes.py` and `tests/test_podcast_schemas.py`. |

#### Phase 1 progress — source of truth decision

- `applepie-ui` is **not** the source of truth for service URLs today. There is no checked-in env surface or base URL config in `package.json`, `app.json`, or `src/`.
- Each backend repo should remain the source of truth for its own runtime env surface through **`.env.example` + `app/config.py`**:
  - `data-ingestion`: owns `GRADIUM_API_BASE_URL`, `QDRANT_URL`, `AZURE_OPENAI_ENDPOINT`, plus related credentials/settings.
  - `story-labeling-api`: owns `QDRANT_URL`, `AZURE_OPENAI_ENDPOINT`, and labeling/clustering settings.
  - `data-provision-api`: owns `DATABASE_URL`, `QDRANT_URL`, storage keys, and podcast-generation settings.
- Phase 2 should introduce a dedicated UI env surface for downstream service URLs instead of hardcoding endpoints inside components.

#### Phase 1 progress — validation matrix

| Hop / service | Local validation | Deployed / live validation |
|---|---|---|
| UI shell | `applepie-ui`: `npm run lint`, `npm run web` | Manual browser validation against the locally or remotely hosted UI until UI automation exists |
| data-ingestion | `uv run pytest`; `docker compose up --build app qdrant`; `curl -fsS http://localhost:8000/health`; `./scripts/smoke_text_ingest.sh` | `RUN_EXTERNAL_SMOKE=1 uv run pytest tests/test_external_smoke.py -k text_only`; `RUN_EXTERNAL_SMOKE=1 uv run pytest tests/test_external_smoke.py -k audio_external`; or direct requests against `https://misleading-dotty-trigub-tech-89f74bab.koyeb.app` |
| story-labeling-api | `uv run --python 3.12 --with-editable . --with pytest --with pytest-asyncio --with httpx pytest`; `uv run uvicorn app.main:app --reload --port 8001`; `curl -fsS http://localhost:8001/health`; `curl -X POST http://localhost:8001/cluster-labels` | Direct requests against `https://ltd-phillie-trigub-tech-9c14c29d.koyeb.app/health` and `/cluster-labels` |
| data-provision-api | `uv run pytest tests/test_main.py tests/test_env_example.py tests/test_podcast_routes.py -q`; `uv run uvicorn app.main:app --reload`; `curl -fsS http://localhost:8000/health`; `curl -fsS http://localhost:8000/universe` | Direct requests against `https://data-provision-api-trigub-tech-7a2fdbc9.koyeb.app/health`, `/universe`, `/podcasts` |

#### Phase 1 decision snapshot

- Phase 1 is now materially advanced: the backend contracts are explicit, the missing caller gaps are explicit, and the validation commands are pinned.
- Phase 1 cleanup applied:
  - `data-ingestion/.env.example` now matches the shipped Gradium path (`/post/speech/asr`).
  - `data-ingestion/README.md` no longer claims the repo is empty, now documents the implemented Gradium contract at a high level, and now describes Qdrant point IDs as UUIDv5 derived from `<input_id>:<chunk_index>`.
  - `data-provision-api/app/main.py` no longer strips `title` metadata out of the OpenAPI schema, and the corresponding route test now checks for valid top-level metadata instead.
  - `story-labeling-api/PLAN.md` now clarifies that `points_updated` includes centroid re-upserts in addition to point payload updates.
- Remaining non-blocking cleanup items:
  - `INGEST_API_KEY` still exists as config/example surface in `data-ingestion`, but route enforcement is not implemented yet; treat that as a deferred auth decision, not a Phase 1 blocker.
  - `applepie-ui` still has no env surface because no backend wiring exists yet; that should be introduced intentionally in Phase 2 rather than guessed during cleanup.
- Readiness status: Phase 1 is now clean enough to begin Phase 2 with a stable contract baseline.

### Phase 2 — Wire the UI to data ingestion
- Add the actual client code in `applepie-ui` to submit recorded audio (and metadata) to `data-ingestion`.
- Make send/error/success states visible in the record flow.
- Persist enough local state so users can retry or see what was sent.
- Outcome: recording becomes a real upstream submission instead of a local-only interaction.
- Exit criteria:
  - from the UI, one record/send action reaches `POST /ingest`
  - the ingest response is surfaced in the UI
  - the same request can be reproduced with the documented local curl/smoke command
- Parallelizable: partially; UI submission plumbing and UI feedback states can be split across files.

#### Phase 2 progress — UI ingest wiring

- `applepie-ui/src/constants/ingest.ts` now defines the Phase 2 UI env surface for ingestion with `EXPO_PUBLIC_INGEST_API_URL` / `EXPO_PUBLIC_INGEST_API_KEY`, defaulting the base URL to the deployed ingestion app so the UI has a real target immediately.
- `applepie-ui/src/features/recording/ingest-client.ts` now implements multipart upload using Expo's file/fetch path: metadata JSON + recorded audio file are posted to `POST /ingest`, and the client normalizes success/error parsing against the Phase 1 contract.
- `applepie-ui/src/features/recording/recording-state.tsx` now owns the upload state machine, expanding status from local-only recording states to `sending | sent | error`, preserving the finalized recording for retry, and storing the ingest result/error centrally.
- `applepie-ui/src/components/app-tabs.tsx`, `src/components/app-tabs.web.tsx`, and `src/features/universe/universe-screen.tsx` now surface the new upload states visibly instead of treating send as a purely local action. The user now gets explicit `Sending…`, `Saved`, and `Retry ready` feedback in the record controls.

#### Phase 2 progress — verification status

- Code-level implementation progress is real, but the Phase 2 exit criteria are **not yet fully satisfied**.
- The workspace TypeScript/Expo tooling is still broken globally in this environment (`expo/tsconfig.base` and package resolution are missing), so static verification is noisy and not trustworthy as a pass/fail signal for this repo.
- Runtime verification uncovered a real backend blocker and one environment blocker:
  - Browser uploads to the deployed ingestion app failed before completion because the service rejected `OPTIONS /ingest` with `405 Method Not Allowed` and had no CORS middleware.
  - The browser session available here has no microphone device (`navigator.mediaDevices.getUserMedia({ audio: true })` returns `NotFoundError`), so the true record-button path still cannot be proven in this environment.
- The backend blocker is now fixed in local code:
  - `data-ingestion/app/main.py` now adds `CORSMiddleware` with origin parsing from `Settings.cors_allow_origins`.
  - `data-ingestion/app/config.py` now exposes `cors_allow_origins` with localhost web defaults.
  - `data-ingestion/tests/test_ingest_contract.py` now includes a preflight contract test.
  - `data-ingestion/.env.example` and `README.md` now document the browser-origin contract.
- Local verification after the fix:
  - `uv run pytest tests/test_ingest_contract.py -q` passed (`13 passed`).
  - Local `OPTIONS /ingest` from origin `http://127.0.0.1:8081` now returns `200` with `access-control-allow-origin`.
  - Local `POST /ingest` with an `Origin` header from `http://127.0.0.1:8081` now returns `200` and includes `access-control-allow-origin`, proving the browser-side network contract is fixed on the backend.
- Phase 2 is therefore **implementation-complete and backend-contract-verified locally**, but still **awaiting one real microphone-backed UI send** to satisfy the original end-to-end record/send exit criteria fully.
- Pending manual verification to perform later on a runtime with a microphone device:
  - start `applepie-ui` against a reachable ingestion target
  - record one real sample through the UI
  - press send and confirm the UI surfaces the ingest JSON success state
  - confirm the request reaches `/ingest` and returns the expected `{ input_id, status, chunks, audio_url? }` contract

### Phase 3 — Trigger labeling after ingest
- Add the orchestration path that calls `story-labeling-api /cluster-labels` after new chunks land in Qdrant.
- Decide whether this is synchronous, background, or manual-trigger only.
- Make failure handling explicit so ingest and labeling can fail independently.
- Outcome: new ingestion can be labeled without a manual external step.
- Exit criteria:
  - after a successful ingest, `POST /cluster-labels` can be triggered through the chosen orchestration path
  - the labeling call returns success and produces observable Qdrant labeling changes or centroid updates
  - failure of labeling is visible without pretending the whole ingest flow succeeded end to end
- Parallelizable: partially; the trigger mechanism and its retry/visibility behavior can be developed separately once the API contract is fixed.

#### Phase 3 progress — ingest-to-label trigger

- `data-ingestion/app/story_labeling.py` now provides a dedicated trigger client for `POST /cluster-labels`, with typed result parsing and isolated timeout / HTTP error handling.
- `data-ingestion/app/config.py` now exposes the Phase 3 trigger surface:
  - `story_labeling_enabled`
  - `story_labeling_api_base_url`
  - `story_labeling_timeout_seconds`
- `data-ingestion/app/main.py` now dispatches the trigger only **after** a successful ingest result is returned from `IngestionService.ingest()`, preserving the intended separation between ingest success and labeling failure.
- The trigger runs as a background side effect and logs labeling failures instead of turning a successful `/ingest` into an ingest error.

#### Phase 3 progress — verification status

- `data-ingestion/tests/test_ingest_contract.py` now covers the new post-success trigger behavior with a fake trigger collaborator.
- `uv run pytest tests/test_ingest_contract.py -q` in `data-ingestion` passed (`13 passed`) after the Phase 3 changes.
- `story-labeling-api/tests/test_main.py` now includes a `/cluster-labels` route contract test with a fake service.
- `PYTHONPATH=. uv run pytest tests/test_main.py -q` in `story-labeling-api` now passes (`6 passed`), so the callee-side route contract is locally verified too.

#### Phase 3 current status

- Phase 3 implementation is materially in place on the ingestion side.
- The remaining high-value verification step is an end-to-end trigger check against a running story-labeling service, confirming that a successful ingest actually causes a real `POST /cluster-labels` call and that labeling failures stay isolated from the ingest response.

### Phase 4 — Wire provisioning consumption in the UI
- Add real reads from `data-provision-api /universe`.
- Add the generate flow that calls `POST /podcasts` and then polls or refreshes podcast status.
- Replace any remaining placeholder universe/story generation assumptions in the UI.
- Outcome: the UI can consume labeled data and launch story/podcast generation from the real backend.
- Exit criteria:
  - the UI renders real data from `GET /universe`
  - the UI can create a podcast job through `POST /podcasts`
  - the UI can observe the resulting podcast state through list/detail refresh or polling
- Parallelizable: yes, universe fetching and podcast generation can be implemented as separate UI client paths.

#### Phase 4 progress — real provisioning consumption

- `applepie-ui/src/constants/provision.ts` now defines the UI env surface for the provisioning API with `EXPO_PUBLIC_PROVISION_API_URL`, defaulting to the deployed data-provision API.
- `applepie-ui/src/features/universe/provision-client.ts` now implements the real `/universe`, `POST /podcasts`, and `GET /podcasts/{id}` client calls and normalizes the returned JSON contracts.
- `applepie-ui/src/features/universe/universe-data.ts` now includes a `hydrateUniverseDataFromApi()` path that converts `UniverseResponse.points/edges` into the existing UI graph shape so the rest of the visual graph system can stay intact.
- `applepie-ui/src/features/universe/universe-screen.tsx` now:
  - loads live `/universe` data on startup
  - shows loading/fallback messaging around live universe fetch state
  - creates real podcast jobs from the existing generation menu
  - polls active podcast jobs until they move out of `pending` / `running`
  - surfaces `Ready`, `Live`, and error states in the action menu instead of treating generation as placeholder-only
- `data-provision-api/app/main.py`, `app/config.py`, `.env.example`, and `tests/test_main.py` now mirror the ingestion-side web fix by adding browser CORS support and a route-level preflight contract test.

#### Phase 4 progress — verification status

- `pytest tests/test_main.py -q` in `data-provision-api` passed (`6 passed`) after the CORS changes.
- `rtk npx expo lint` in `applepie-ui` is now down to one pre-existing hook-dependency warning in the star-particle animation logic; there are no lint errors in the newly added provisioning code.
- The deployed provision API still rejected browser fetches during this session because it has not yet been redeployed with the new CORS support, so the web UI cannot fully prove live `/universe` consumption against the deployed host until that deployment happens.

### Phase 5 — End-to-end verification and cleanup
- Run one end-to-end pass through all four repos.
- Add/adjust tests for each hop and one smoke test for the full chain.
- Clean up stale docs/env examples so they match the implemented flow.
- Outcome: the chain is verifiable, documented, and no longer depends on stale notes.
- Exit criteria:
  - one sample input can be followed from UI submission through ingest, labeling, provisioning read, and podcast generation
  - the same path can be checked locally, and at least the highest-signal deployed checks can be replayed against the live services
  - docs and smoke commands are updated so the feedback loop survives after this session
- Parallelizable: limited; verification should be mostly sequential, but test scaffolding can be prepared in parallel with implementation.

#### Phase 5 progress — verification/docs updates completed

- `story-labeling-api/tests/test_main.py` now covers the full `/cluster-labels` route contract:
  - happy path (`200`)
  - `ClusteringError -> 400`
  - `LabelingError -> 502`
  - `VectorStoreError -> 503`
  - generic error -> `500`
- `PYTHONPATH=. uv run pytest tests/test_main.py -q` in `story-labeling-api` passed (`6 passed`).
- `applepie-ui/README.md` now documents:
  - the UI env vars for ingest/provision
  - the live features already wired in the UI
  - the exact remaining manual runtime verification checklist
  - the external blockers that still require real services or hardware

#### Remaining manual work to complete the integration

1. Redeploy the changed backend services so the live hosts match the local code:
   - `data-ingestion` (for browser CORS + Phase 3 trigger support)
   - `data-provision-api` (for browser CORS + live provisioning support)
2. Run one microphone-backed UI verification on a runtime with a real audio device:
   - open `applepie-ui`
   - grant microphone permissions
   - record a real sample
   - press send
   - confirm the UI shows the ingest success state and `/ingest` returns the expected JSON
3. Run one real Phase 3 integration check with a reachable story-labeling deployment:
   - perform a successful ingest
   - confirm it triggers `POST /cluster-labels`
   - confirm a labeling failure, if induced, does not break the `/ingest` response
4. Run one real Phase 4/5 generation check with live provision dependencies:
   - confirm `/universe` loads in the UI from the deployed provision API
   - create a real podcast job from the action menu
   - confirm the job transitions through `pending/running/completed` or `failed`
   - confirm the final audio / cover / script data are retrievable from the provision API

#### End-to-end implementation status

- All remaining **code-side** integration steps requested in this session are now implemented.
- What remains is **deployment-backed and hardware-backed runtime verification**, not missing application code.

#### Handoff status for commit/push

- The current repo state should be treated as the integration baseline for the next runtime verification pass.
- Code changes are ready to be committed per repo.
- After pushing, the next human-driven steps are redeploying the changed backend repos and running the manual runtime checklist above.

## 2026-05-16 manual QA review

- The podcast domain code is well-covered at the unit level: targeted podcast tests pass, full `pytest` passes, and `lsp_diagnostics` reported zero Python diagnostics in `app/`.
- The generation pipeline itself is coherent for demo scope: repository state machine, chunk extraction, script bounding, TTS synthesis, audio merge, blob upload, and best-effort cover fallback are all explicitly tested.
- The biggest remaining QA risk is integration wiring, not unit logic: startup/bootstrap and background-task resource ownership are the parts most likely to fail for the first real user.

## 2026-05-16 F1 compliance blocker fix

- `POST /podcasts` now returns `202`, trims/rejects blank labels at schema validation, bootstraps the real `podcasts` table via `get_podcast_repository()`, and routes real background generation through a settings-owned Qdrant client instead of reusing a request-yielded reader.
- `.env.example` now includes an `az postgres flexible-server create` template, and the focused route/env tests were updated to lock in the API contract and placeholder-only setup docs.
- Remaining hackathon risk: background jobs are still FastAPI `BackgroundTasks`, so they are intentionally non-durable across process restarts even though resource ownership is now correct.

## 2026-05-16 cleanup

- Removed generated `__pycache__`, `.pyc`, and `.idea/` artifacts from the working tree and added ignore rules so compile/test runs do not reintroduce them.

## 2026-05-16 typing verification cleanup

- `app/podcast_generation.py` now reuses the shared `app.audio.merge_audio_clips` helper instead of shadowing it locally, and `synthesize_script_audio` accepts the protocol-based TTS dependency used by `generate_podcast`.
- `app/db.py` now exposes a small structural connection protocol for bootstrap typing, `app/podcast_repository.py` uses the narrower psycopg query cast Pyright expects, and the podcast tests now match the production callable signatures without changing behavior.
- Verification after the cleanup: project-level Python `lsp_diagnostics` reported zero errors, focused podcast/env pytest passed, full `pytest` passed, and `python -m compileall app tests` passed.

## 2026-05-16 repository lifecycle fix

- `PodcastRepository` now tracks whether it owns the underlying DB connection and only closes self-owned connections, which keeps injected fake/test connections untouched while giving real request/background paths an explicit cleanup hook.
- `get_podcast_repository()` is now a yielded dependency that always closes the real repository in `finally`, and the real `run_podcast_generation_from_settings()` path creates and closes its own repository instead of extending the request-scoped repository into the background task.
- Verification after the lifecycle fix: project-level Python `lsp_diagnostics` reported zero errors, focused lifecycle pytest passed (`19 passed`), full `pytest` passed (`46 passed`), and `python -m compileall app tests` passed.
