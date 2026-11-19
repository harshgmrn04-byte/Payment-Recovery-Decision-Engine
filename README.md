# Payment Recovery Decision Engine

A local FastAPI backend that classifies failed card payments into `retry_now`,
`schedule_retry`, `notify_customer`, `manual_review`, or `mark_lost` workflows.
It compares recovery strategies against a retry-all baseline and records
idempotent recovery runs, metrics, and payment webhooks in PostgreSQL.

## What is implemented

- Deterministic `rules` and `retry_all` strategies, plus optional Gemini/Groq
  classification with a rules fallback.
- A deterministic mock gateway, so local runs and evaluations are repeatable.
- PostgreSQL persistence for loaded failures, agent runs, webhook events, and
  recovered revenue metrics.
- Database-backed idempotency: a run is reserved before gateway execution and
  the same transaction/action key is never retried.
- A FastAPI API with health, ingestion, agent-run, metrics, recent-run,
  authenticated mock-gateway, and webhook endpoints.

The IBM dataset labels are a portfolio-project proxy for real payment outcomes:
they are derived from `Errors?`, not confirmed bank recovery results. See
[the project write-up](docs/fde_writeup.md) for the assumptions and trade-offs.

## Project layout

```text
recovery_agent/
  app.py          # FastAPI endpoints
  agent.py        # planning, idempotency keys, orchestration
  ingest.py       # IBM CSV parsing and PostgreSQL loading
  db.py           # PostgreSQL repository and metrics
  gateway.py      # deterministic mock payment gateway
  llm.py          # optional Gemini/Groq classifier
  rules.py        # deterministic strategies
  schemas.py      # domain and API models
scripts/
  evaluate.py     # offline strategy comparison
tests/            # unit and API tests
docs/             # scope and project write-up
```

## Run locally (no Docker)

Prerequisites: Python 3.9+ and a running local PostgreSQL server. Create an
empty database once:

```bash
createdb failed_payments
```

Create the environment and install the dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cp .env.example .env
```

Set `DATABASE_URL` in `.env` if your PostgreSQL user, password, port, or host
is not represented by the default. Point `IBM_ARCHIVE_DIR` at the directory
that contains `credit_card_transactions-ibm_v2.csv`; for example:

```dotenv
IBM_ARCHIVE_DIR=/absolute/path/to/archive
DATABASE_URL=postgresql://localhost:5432/failed_payments
```

Load a reproducible sample into PostgreSQL:

```bash
python -m recovery_agent.ingest --sample-size 100000
```

Start the API:

```bash
uvicorn recovery_agent.app:app --reload
```

Open [interactive API documentation](http://127.0.0.1:8000/docs), or run the
rule-based agent:

```bash
curl -X POST http://127.0.0.1:8000/agent/run \
  -H 'Content-Type: application/json' \
  -d '{"mode":"rules","limit":500,"reset_runs":true}'

curl http://127.0.0.1:8000/metrics
```

`reset_runs` clears only previous local agent-run and webhook records; it does
not modify the ingested `failed_transactions` source data. Subsequent requests
with the same transaction/action are safely skipped by the idempotency key.

## API endpoints

| Endpoint | Purpose |
| --- | --- |
| `GET /health` | Liveness check; does not need PostgreSQL. |
| `POST /ingest` | Load a sampled IBM dataset through the API. |
| `POST /agent/run` | Process previously unprocessed transactions. |
| `GET /metrics` | Recovery-rate, revenue, confidence, and wasted-retry metrics. |
| `GET /runs` | Recent persisted recovery runs. |
| `POST /gateway/retry` | Authenticated deterministic mock-gateway call. |
| `POST /webhooks/payment` | Store a duplicate-safe payment webhook. |

`/gateway/retry` requires the `X-Gateway-Token` header configured in `.env`.
The recovery agent itself invokes the gateway in process, not through HTTP.

## Offline evaluation

This path reads the CSV directly and does not need PostgreSQL:

```bash
python -m scripts.evaluate --sample-size 5000 --modes retry_all,rules
```

To include an LLM, configure a Gemini or Groq key in `.env` and keep the
sample small to avoid unnecessary API cost:

```bash
python -m scripts.evaluate --sample-size 100 --modes retry_all,rules,llm
```

The output reports classification accuracy, recovery rate, false-retry rate,
recovered revenue, and which classifier handled each row.

## Verification

```bash
python -m unittest discover -s tests -v
```

No Docker or deployment files are included. The dataset is intentionally kept
outside the repository; it can be placed in an ignored `data/` directory or
referenced through `IBM_ARCHIVE_DIR`.
