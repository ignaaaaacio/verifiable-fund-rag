-- FundRAG initial schema (PostgreSQL 15+ with pgvector, e.g. Supabase).
-- Run once in the Supabase SQL editor, or: psql "$DATABASE_URL" -f db/migrations/001_init.sql

CREATE EXTENSION IF NOT EXISTS vector;

-- Every value and passage points back to the filing it came from.
CREATE TABLE IF NOT EXISTS filings (
    id               BIGSERIAL PRIMARY KEY,
    accession_number TEXT NOT NULL UNIQUE,      -- SEC filing number
    cik              TEXT NOT NULL,             -- registrant
    filed_at         DATE NOT NULL,
    taxonomy         TEXT NOT NULL,             -- 'rr' or 'oef'
    data_release     TEXT NOT NULL,             -- e.g. '2026_10'
    is_current       BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS funds (
    series_id TEXT PRIMARY KEY,                 -- e.g. S000002277
    cik       TEXT NOT NULL,
    name      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS share_classes (
    class_id  TEXT PRIMARY KEY,                 -- e.g. C000005953
    series_id TEXT NOT NULL REFERENCES funds(series_id),
    name      TEXT NOT NULL,
    ticker    TEXT
);

-- Tier 1: tagged numbers (the answer key).
CREATE TABLE IF NOT EXISTS facts (
    id        BIGSERIAL PRIMARY KEY,
    filing_id BIGINT NOT NULL REFERENCES filings(id),
    series_id TEXT REFERENCES funds(series_id),
    class_id  TEXT REFERENCES share_classes(class_id),
    tag       TEXT NOT NULL,                    -- internal tag after RR/OEF mapping
    raw_tag   TEXT NOT NULL,                    -- tag as filed
    value     NUMERIC NOT NULL,
    unit      TEXT,                             -- 'pure', 'USD', ...
    period    TEXT,                             -- e.g. '1y', '5y', '2025'
    label     TEXT
);
CREATE INDEX IF NOT EXISTS facts_lookup ON facts (class_id, tag);
CREATE INDEX IF NOT EXISTS facts_series ON facts (series_id, tag);

-- Tier 2: text passages (cited evidence).
CREATE TABLE IF NOT EXISTS passages (
    id        BIGSERIAL PRIMARY KEY,
    filing_id BIGINT NOT NULL REFERENCES filings(id),
    series_id TEXT REFERENCES funds(series_id),
    class_id  TEXT REFERENCES share_classes(class_id),
    section   TEXT NOT NULL,                    -- 'strategy', 'principal_risk', ...
    content   TEXT NOT NULL,
    embedding vector(384),                      -- bge-small-en-v1.5
    tsv       tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED
);
CREATE INDEX IF NOT EXISTS passages_embedding_hnsw
    ON passages USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS passages_tsv ON passages USING gin (tsv);

-- Questions, answers, and what each answer cites.
CREATE TABLE IF NOT EXISTS questions (
    id         BIGSERIAL PRIMARY KEY,
    text       TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS answers (
    id          BIGSERIAL PRIMARY KEY,
    question_id BIGINT NOT NULL REFERENCES questions(id),
    route       TEXT NOT NULL,                  -- 'number', 'text', 'mixed'
    status      TEXT NOT NULL,                  -- 'answered', 'insufficient_evidence'
    body        JSONB NOT NULL,                 -- claims after the gates
    latency_ms  INTEGER,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS citations (
    answer_id  BIGINT NOT NULL REFERENCES answers(id),
    passage_id BIGINT REFERENCES passages(id),
    fact_id    BIGINT REFERENCES facts(id),
    CHECK (passage_id IS NOT NULL OR fact_id IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          BIGSERIAL PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    user_id     TEXT,
    endpoint    TEXT NOT NULL,
    question_id BIGINT REFERENCES questions(id),
    timings_ms  JSONB                           -- per-step latency
);

CREATE TABLE IF NOT EXISTS eval_runs (
    id             BIGSERIAL PRIMARY KEY,
    started_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    model          TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    data_release   TEXT NOT NULL,
    thresholds     JSONB NOT NULL,
    seed           INTEGER NOT NULL,
    results        JSONB
);
