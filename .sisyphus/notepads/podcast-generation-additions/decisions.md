## 2026-05-16 Task 2
- Kept the persistence layer intentionally small: one `podcasts` table, one connection helper, and one repository class with explicit lifecycle methods.
- Did not extend the public podcast schemas with lifecycle timestamps; instead, timestamps are persisted in Postgres and verified in repository tests.
