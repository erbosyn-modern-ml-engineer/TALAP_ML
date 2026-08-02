# TALAP — WhatsApp MVP

TALAP is a backend and AI orchestration service for a universal commerce
assistant. This repository currently implements a **WhatsApp-only backend MVP**:
a customer messages a WhatsApp business number, TALAP extracts a structured
request, searches the product catalog by vector similarity, recommends up to
three products, lets the customer pick one by replying `1`, `2` or `3`, confirms
the choice with a configured manager link, and records requests it cannot
fulfill.

## Implemented flow

1. WhatsApp webhook: verification endpoint + HMAC signature validation.
2. Inbound normalization and idempotent PostgreSQL ingestion (one message, one
   processing job per event).
3. Worker claims the job and runs it outside any open transaction.
4. DeepSeek extracts a validated structured `CustomerRequest` (fake in tests).
5. pgvector product search (Jina embeddings) returns up to three available
   products.
6. Numbered plain-text reply (`Нашёл подходящие варианты:`), and the displayed
   set is persisted per customer.
7. Reply `1` / `2` / `3` resolves the displayed product, marks the state
   selected, and sends the product name, price, and the configured manager
   link — without calling DeepSeek or product search again.
8. Zero results persist one unmet-demand record and send a concise response.
9. Voice/image/unsupported messages complete without an outbound reply.

## Architecture

- FastAPI (webhook routes, health).
- PostgreSQL + SQLAlchemy 2 (async) with tenant-safe models.
- Alembic migrations (one revision per MVP, `-x database=main|test`).
- pgvector (`product_embeddings`, cosine distance) with the Jina embedding
  provider (`jina-embeddings-v5-text-small`, 1024 dims).
- DeepSeek structured extraction (`deepseek-v4-flash`).
- WhatsApp Cloud API outbound (`send_text`, one account).

## Requirements

- Python `>=3.12,<3.14` (target: 3.12).
- PostgreSQL with the `pgvector` extension.
- A venv + pip (project is a Python package; no third-party package manager).

## Setup

```powershell
git clone https://github.com/erbosyn-modern-ml-engineer/TALAP_ML.git
cd TALAP_ML
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
Copy-Item .env.example .env   # then edit .env with real values
python -m alembic -x database=main upgrade head
```

The demonstration catalog lives in `data/demo/demo_catalog.csv` (synthetic
white school-shirt data, no personal information). Import it per merchant
through the catalog API (a merchant must exist first; use its UUID):

```powershell
$body = Get-Content -Raw data\demo\demo_catalog.csv
Invoke-RestMethod -Method Post `
  -Uri "http://localhost:8000/api/v1/merchants/<MERCHANT_UUID>/catalog/import" `
  -Headers @{ "X-Internal-Service-Token" = $env:INTERNAL_SERVICE_TOKEN } `
  -Form @{ file = Get-Item data\demo\demo_catalog.csv }
```

## Run API

```powershell
uvicorn apps.api.main:app --reload
```

## Run worker

```powershell
python -B -c "import asyncio; from apps.worker.main import run_whatsapp_echo_once; asyncio.run(run_whatsapp_echo_once())"
```

## Tests

```powershell
$env:PYTHONDONTWRITEBYTECODE="1"
python -B -m pytest -q -p no:cacheprovider --tb=short
```

Focused local end-to-end test:

```powershell
python -B -m pytest tests/integration/e2e/test_whatsapp_e2e.py -q -p no:cacheprovider --tb=short
```

## Local E2E

- DeepSeek and WhatsApp HTTP are **faked**; no Meta, DeepSeek, or Jina API is
  called in tests.
- PostgreSQL is required (integration tests target `talap_test`).
- Successful selection, no-product, and duplicate-webhook paths are covered.

## Live WhatsApp limitation

- A real Meta webhook requires a **public HTTPS URL**; local development cannot
  receive real Meta callbacks.
- Real Meta/WhatsApp live testing is outside this local handoff and is **not**
  claimed by this repository's tests.

## Security

- Never commit `.env`; only `.env.example` with placeholders is tracked.
- Webhook signatures are verified before ingestion.
- Logs and errors redact secrets; no API keys, tokens, or raw bodies are
  exposed.
- Demonstration data is synthetic.
- The repository is temporarily **public** (for a teammate), so only
  placeholders may be committed.

## Deferred scope

- Telegram integration.
- Voice / GigaAM.
- Redis / ARQ workers.
- Interactive WhatsApp Flows / buttons.
- Large conversation state machine.
- Demand clustering / radar.
- Multi-manager routing.
- Production deployment.
