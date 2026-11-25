# FundRAG

Verifiable Retrieval-Augmented Analysis of Mutual Fund Costs and Risks.
UPRM CIIC/INSO 4151 Capstone, Fall 2026.

FundRAG answers questions about mutual fund fees, returns and risks using SEC prospectus data.
Every answer cites the filing it came from, and four checks (gates) block answers that are
not supported by the evidence.

## Status

| Milestone | Target | State |
| --- | --- | --- |
| Scaffold (API, DB schema, CI) | Nov 24 | done |
| M0 data spike | Nov 26 | script ready, run locally |
| M1 data load + RR/OEF mapping | Nov 26 | not started |
| FundQA-150 locked | Nov 28 | not started |
| Progress report | Nov 29 | not started |
| M3 MVP (Ask: number + text paths, gates G1/G2/G4) | Dec 1 | not started |
| M4 Compare, Record, G3, cost calculator | Dec 4 | not started |
| Evaluation + expert sessions | Dec 4–8 | not started |
| Private presentation | TBD with advisor | not started |

## Repository layout

```
backend/            FastAPI app (Python 3.12)
  app/api/          REST routes under /v1
  tests/            pytest
db/migrations/      SQL for Supabase Postgres + pgvector
scripts/            data tools (sec_spike.py, loaders)
eval/               FundQA-150 and evaluation runner (later)
frontend/           React app (later)
docs/decisions/     architecture decision records (ADRs)
```

## Run the backend locally

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp ../.env.example .env        # then fill in DATABASE_URL and OPENAI_API_KEY
uvicorn app.main:app --reload
```

Open http://localhost:8000/v1/health and http://localhost:8000/docs.
Without a `.env` the API still starts and uses a local SQLite file, so `/v1/health` reports
`database: true` but `llm_configured: false`.

Run the tests:

```bash
cd backend && pytest -q
cd .. && pytest -q scripts
```

## Set up the database (Supabase)

1. Create a Supabase project.
2. In the SQL editor, run `db/migrations/001_init.sql` (it enables `vector`).
3. Copy the connection string (Settings → Database) into `DATABASE_URL` in `backend/.env`,
   using the `postgresql+psycopg://` prefix.

## M0 data spike

The SEC asks for a name and email in the User-Agent of every download, so run this on your own
machine:

```bash
python scripts/sec_spike.py --url <zip URL from the SEC Risk/Return data-set page> \
    --user-agent "Your Name your@email.com"
# or, after downloading the zip in a browser:
python scripts/sec_spike.py --zip ~/Downloads/<file>.zip
```

It writes `reports/spike_report.md`: the files and columns in the zip, the taxonomy versions
(RR vs OEF), the fee, expense-example, return and risk tags it found, and how many funds and
share classes have each. That report decides the RR/OEF mapping for the loader.

## Secrets

Never commit `.env`. It is in `.gitignore`. CI does not need any secrets yet.
