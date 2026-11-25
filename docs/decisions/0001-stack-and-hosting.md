# ADR 0001: Stack and hosting

- Status: accepted
- Date: 2026-11-24

## Context

Solo project, about 18 days of build and evaluation before the final presentation. The proposal
fixes the stack: FastAPI, PostgreSQL with pgvector, React. We need hosting that is free or
cheap, needs little operations work, and lets the advisor open a live demo.

## Decision

- Backend: FastAPI (Python 3.12), stateless, REST under `/v1`, OpenAPI generated automatically.
- Database: Supabase Postgres with the `vector` extension. Structured facts (Tier 1) and text
  passages with embeddings (Tier 2) live in the same database, so one query can join them.
- Search index: HNSW on `vector(384)` for bge-small-en-v1.5 embeddings, plus a GIN index on a
  generated `tsvector` for keyword search.
- Backend hosting: Render. Frontend hosting: Vercel. CI: GitHub Actions.
- Configuration through environment variables (`pydantic-settings`); local runs fall back to
  SQLite so the API and tests work with no accounts.

## Consequences

- One database to back up and inspect; the eval runs can store their settings next to the data.
- SQLite fallback cannot run pgvector queries, so retrieval tests will need Postgres
  (planned: a CI service container when the retrieval code lands).
- Render's free tier sleeps when idle; warm it up before demos.
