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
