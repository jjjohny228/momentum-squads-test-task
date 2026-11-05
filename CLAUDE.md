# CLAUDE.md

Guidance for AI agents working in this repo.

## Project
Support-ticket service: helpdesk webhook -> RAG (PGVector + OpenAI embeddings) -> LLM draft
(LangChain + OpenAI) -> operator moderation (Jinja2 + HTMX UI / JSON API) -> reply via helpdesk API.
Plan and design decisions: `docs/architecture.md`.

## Commands
- Install: `uv sync`
- Lint/format: `uv run ruff check . && uv run ruff format --check .`
- Types: `uv run mypy`
- Tests (need Postgres from compose): `docker compose up -d db && uv run pytest`
- Run everything: `docker compose up --build`

## Layout
Domain modules in `app/` (`tickets`, `knowledge`, `drafting`, `helpdesk`, `jobs`, `ui`, `health`);
each is `router -> service -> repository -> models`, Pydantic `schemas` are the API contract.
Cross-cutting code lives in `app/core`.

## Rules
- Webhook handler must stay fast (<2s): only validate + insert ticket + enqueue job in one transaction.
- Never call the LLM or helpdesk inside an HTTP request; enqueue a job instead.
- Every request to OpenAI (chat and embeddings, including retries and keyless mode) goes through
  `app/drafting/rate_limiter.py`.
- Status transitions are conditional UPDATEs (`WHERE status = ...`) - never read-then-write.
- Transactions are owned by services; repositories never commit.
- Keep mypy strict and ruff clean; add tests for every behavior change.
- Migrations are hand-written in `alembic/versions`.
