## 2026-05-16 manual QA review

- `POST /podcasts` passes a dependency-yielded `QdrantPointReader` into `BackgroundTasks` (`app/main.py:75-85`, `app/main.py:165-171`). FastAPI's current guidance says background tasks should create their own resources instead of reusing yielded dependency resources, so this wiring is not reliable for real requests.
- The app never bootstraps the `podcasts` table at startup or before repository writes. `init_db()` exists in `app/db.py` and `PodcastRepository.init_db()` exists in `app/podcast_repository.py`, but no application path calls either one.
- Route tests fake out both the repository and the point reader (`tests/test_podcast_routes.py`), so they do not exercise the real database/table bootstrap path or the real Qdrant dependency lifecycle.
- `POST /podcasts` returns `201` (`app/main.py:156`) even though the feature is documented and implemented as a background-job style flow; this is a smaller API contract mismatch but still user-facing.
- Azure PostgreSQL setup docs are incomplete for a new hackathon deployer. `.env.example` documents the connection string format, but there is no checked-in `az postgres flexible-server ...` setup command.

## 2026-05-16 final audit

- Hard blockers: `POST /podcasts` still returns `201` (`app/main.py:156`) instead of the plan's required `202`, blank `label` is only typed as `str` with no validation (`app/podcast_schemas.py:9-13`), and the app still has no startup/bootstrap path that calls `init_db()` (`app/main.py:70-190`, `app/db.py:41-49`).
- Integration risk remains: `get_point_reader()` yields the Qdrant client resource and closes it after the request (`app/main.py:75-85`), but the background task receives that dependency object (`app/main.py:165-171`), so the task is still coupled to request-scoped resource lifetime.
- Docs gap remains: `.env.example` documents the Azure Blob container command (`.env.example:9-13`) but does not include the required `az postgres flexible-server create` setup template.
