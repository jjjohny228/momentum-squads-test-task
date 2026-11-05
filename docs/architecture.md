# Architecture & implementation plan

> Approved plan this repo was built from (written with Claude Code in plan mode). Deviations found during implementation are listed at the end.


## Context
Тестовое задание (AI-first Backend Engineer, 3–4 ч). Сервис: хелпдеск шлёт обращение на вебхук → сервис ищет статьи в базе знаний (RAG) → LLM генерирует черновик со ссылками → оператор в веб-UI подтверждает/отклоняет → подтверждённый ответ уходит клиенту через API хелпдеска (мок).

Жёсткие требования, которые определяют архитектуру:
- вебхук отвечает < 2 с, дубликаты событий (в т.ч. одновременные), пики 500/10 мин, нельзя терять обращения;
- LLM 5–30 с на вызов, лимит 60 rpm на весь сервис;
- режим без API-ключа: реальный запрос к провайдеру всё равно делается, ответ подменяется статическим с задержкой 5–30 с;
- хелпдеск отдаёт 5xx/таймауты, клиент не должен получить ответ дважды;
- `GET /health` → 200 `OK`; запуск `docker compose up`.

Проект пустой (`main.py`, `pyproject.toml` — шаблон PyCharm, заменяем). Деплой пока не делаем; CI есть, CD нет.

## Стек
| Слой | Выбор | Почему |
|---|---|---|
| Пакеты | **uv** (lock-файл, `uv sync --frozen`) | быстрый, воспроизводимый |
| Lint/format | **ruff** (check + format), **mypy** strict на `app/` | одна утилита, строгие типы |
| API | **FastAPI** + Pydantic v2 + pydantic-settings | по ТЗ |
| БД | PostgreSQL 16 + **pgvector** (`pgvector/pgvector:pg16`) | одна БД для данных, очереди и векторов |
| ORM/миграции | SQLAlchemy 2.0 async + **psycopg 3** + **Alembic** | один драйвер и для SQLAlchemy, и для langchain-postgres |
| Очередь | **очередь задач в Postgres** (`SELECT … FOR UPDATE SKIP LOCKED`, lease) | транзакционно с приёмом вебхука → ничего не теряем; без Redis/брокера. Celery/RabbitMQ отклонены: для той же гарантии нужен outbox, нагрузка ≤ 50/мин, Celery не async-native, его rate_limit per-worker (не глобальный). Описать в README как осознанный компромисс + путь миграции на Taskiq/Redis при росте |
| LLM | **LangChain** + `langchain-openai` (`ChatOpenAI`, модель из env `OPENAI_MODEL`, по умолчанию **`gpt-5.4-mini`**) | серия 5, mini-тир — оптимум цена/качество для коротких RAG-ответов; `reasoning_effort=low` для скорости. Перед стартом сверить id и цену на странице OpenAI pricing |
| Векторный поиск | `langchain-postgres` **PGVector** | по ТЗ |
| Эмбеддинги | **OpenAI `text-embedding-3-small`** (`OpenAIEmbeddings`, 1536 dim) | решение пользователя; мультиязычная, дешёвая. Keyless-поведение — см. решение 6a |
| Ретраи | **tenacity** | backoff для LLM и отправки ответа |
| UI | **Jinja2 + HTMX** (CDN) внутри FastAPI | минимум, без сборки JS |
| Тесты | pytest, pytest-asyncio, httpx `AsyncClient`, **testcontainers[postgres]** локально / service-контейнер в CI | реальный PG для SKIP LOCKED / ON CONFLICT |
| Логи | structlog (JSON) | корреляция по `ticket_id`/`event_id` |

## Архитектура
```
Helpdesk(mock) --POST /webhooks/helpdesk--> api ──tx: INSERT ticket ON CONFLICT(event_id) DO NOTHING
                                                  + INSERT job(generate_draft)──> 202 (мс)
worker (N asyncio-слотов) ── claim job SKIP LOCKED ──> retrieve (PGVector) ──> rate limiter ──> LLM ──> draft
Operator UI ── approve ──> tx: status=approved + job(send_reply)
worker ── send_reply ──> POST reply_url (Idempotency-Key) ──> 201 → sent | 5xx/timeout → retry backoff
```
Сервисы docker compose: `db`, `migrate` (alembic upgrade + сид базы знаний, one-shot), `api` (uvicorn), `worker` (`python -m app.worker`), `mock-helpdesk`.

### Ключевые решения
1. **Вебхук < 2 с и без потерь.** Хендлер только валидирует и в одной транзакции пишет `tickets` + `jobs`. Никакого LLM в запросе. Возвращает `202 Accepted` (и для дубликата тоже 200/202 — чтобы хелпдеск перестал ретраить).
2. **Дубликаты, в т.ч. одновременные.** `UNIQUE(event_id)` + `INSERT … ON CONFLICT DO NOTHING RETURNING id`; job создаётся только если вставка реально произошла. Гонку решает БД, не код.
3. **Очередь `jobs`**: `id, kind, ticket_id, status(queued|running|done|failed), attempts, max_attempts, run_after, locked_until, last_error`. Claim: `UPDATE … WHERE id = (SELECT … WHERE status='queued' AND run_after<=now() OR (status='running' AND locked_until<now()) FOR UPDATE SKIP LOCKED LIMIT 1)`. Lease (`locked_until`) — восстановление после падения воркера. Ошибка → `run_after = now()+backoff`, после `max_attempts` → `failed`, видно в UI с кнопкой «retry». Пробуждение: `LISTEN/NOTIFY` + поллинг раз в 1 с как fallback.
4. **Пропускная способность.** Пик 50 обращ./мин × до 30 с = ~25 одновременных вызовов → воркер с `WORKER_CONCURRENCY=30` asyncio-слотов (I/O-bound). Лишнее ждёт в очереди (см. 6b).
5. **Лимит 60 rpm на весь сервис** — распределённый, в Postgres (не in-memory, чтобы работал при нескольких репликах воркера): таблица `llm_calls(ts)` + `pg_advisory_xact_lock`; перед вызовом считаем записи за последние 60 с, если ≥ лимита — ждём до освобождения окна. Считается каждый запрос к провайдеру, включая keyless-режим и ретраи.
6. **Keyless-режим.** Обёртка `DraftLLM` над LangChain-цепочкой: если `OPENAI_API_KEY` пуст — вызов идёт к OpenAI с placeholder-ключом (реальный HTTP-запрос, ожидаемый 401), исключение ловится, возвращается статический черновик (по найденным статьям, со ссылками) после `asyncio.sleep(random.uniform(5, 30))`. Дальше по пайплайну — тот же объект `Draft`. С ключом: ретраи на 429/5xx/timeout (tenacity), timeout 60 с.
6a. **Эмбеддинги и keyless.** Вектора статей считаются один раз скриптом `scripts/embed_kb.py` (нужен ключ) и **коммитятся** в `kb/embeddings.json` (+ хэш контента и имя модели). Сид грузит их через `PGVector.aadd_embeddings(texts, embeddings, metadatas, ids)` — без вызова API, поэтому БД наполняется и без ключа, а в CI детерминированно. Если хэш статьи не совпадает с сохранённым и ключ есть — пересчитываем. Эмбеддинг запроса (текст обращения) — реальный вызов `text-embedding-3-small`: с ключом → векторный поиск; без ключа → реальный запрос (401) и fallback на полнотекстовый поиск Postgres (`tsvector`, конфиг `simple`, GIN-индекс) по тем же статьям. Retriever реализован как один интерфейс `KnowledgeRetriever`, стратегия выбирается автоматически.
6b. **Лимит и эмбеддинги.** Запрос эмбеддинга — тоже запрос к провайдеру, поэтому по умолчанию идёт через тот же лимитер (консервативно; `RATE_LIMIT_INCLUDE_EMBEDDINGS=true`). Это 2 запроса на обращение → ~30 обращений/мин; пик 500/10 мин разбирается из очереди за ~17 мин без потерь. Флагом можно вынести эмбеддинги в отдельный бакет. Описать компромисс в README.
7. **Генерация.** LCEL-цепочка: `retriever(k=3) → prompt (system: отвечай только по статьям, на языке клиента, цитируй ссылки) → ChatOpenAI.with_structured_output(Draft)`, где `Draft{answer: str, cited_article_ids: list[str]}`. Ссылки на статьи (`/kb/{slug}`) рендерятся из метаданных, а не доверяются LLM. Если релевантных статей нет (score ниже порога) — черновик помечается «нужен ручной ответ».
8. **Ответ клиенту ровно один раз.** Машина состояний `received → generating → draft_ready → approved → sending → sent` (+ `rejected`, `failed`). Approve — условный `UPDATE … WHERE status='draft_ready'` (двойной клик/две вкладки → только один job). Отправка с заголовком `Idempotency-Key: reply-<ticket_id>`; ретраи только на 5xx/timeout/сетевые ошибки, 4xx → `failed`. Честная оговорка в README: при таймауте мы не знаем, дошёл ли запрос, поэтому exactly-once невозможен без дедупликации на стороне получателя — мок её реализует по ключу; в реальной интеграции требуем такую же гарантию от API хелпдеска.
9. **Мок хелпдеска** (отдельный FastAPI-сервис): `POST /tickets/{id}/replies` случайно отдаёт 5xx / зависает > timeout (вероятности через env), дедуплицирует по `Idempotency-Key`, хранит полученные ответы (`GET /replies` для проверки). Плюс скрипт `scripts/simulate.py`: шлёт N вебхуков, включая повторы и одновременные дубликаты, с 2-секундным таймаутом и ретраями как настоящий хелпдеск.
10. **База знаний.** 5–10 статей markdown (вымышленный продукт, сгенерированы AI) в `kb/articles/*.md` с YAML front-matter. **Метаданные проставляются при загрузке в БД** (`scripts/seed_kb.py`): для каждого `Document` → `metadata = {article_id/slug, title, url: /kb/{slug}, category, tags, language, chunk_index, content_hash, updated_at}`. Статьи режутся `RecursiveCharacterTextSplitter` (по заголовкам markdown), метаданные статьи копируются в каждый чанк. Сид идемпотентный: `ids = f"{slug}:{chunk_index}"`, upsert; по `content_hash` пропускаем неизменённые. Retrieval возвращает метаданные → из них строятся ссылки в черновике и фильтры (`filter={"language": ...}` при необходимости). Страница `/kb/{slug}` для ссылок.
11. **Бэкенд не зависит от UI.** Слои: `api` (тонкие роутеры) → `services` (бизнес-логика, переходы статусов) → `db`. Полноценный JSON REST API `/api/v1` с OpenAPI/Swagger (`/docs`): `GET /tickets?status=`, `GET /tickets/{id}`, `POST /tickets/{id}/approve` (опц. отредактированный текст), `POST /tickets/{id}/reject`, `POST /tickets/{id}/retry`. HTML-роуты UI — второй тонкий адаптер над теми же сервисами (возвращают HTML-фрагменты для HTMX вместо JSON). Можно заменить UI на SPA без правок бэкенда. Тесты бизнес-логики идут через JSON API/сервисы.
12. **UI оператора**: `/` — список обращений с фильтром по статусу и автообновлением (HTMX polling), карточка: сообщение клиента, черновик (редактируемый textarea), найденные статьи, кнопки Approve/Reject/Retry. Basic Auth через env (опционально).

## Архитектура FastAPI-приложения
Модульная (по доменам, в стиле *fastapi-best-practices*) + слоистая внутри модуля:
`router` (HTTP, валидация, коды ответов) → `service` (бизнес-логика, транзакции, переходы статусов) → `repository` (SQL-запросы, без логики) → `models` (SQLAlchemy ORM). Pydantic `schemas` — контракт API, ORM наружу не отдаём.
- **DI через `Depends`**: `get_session` (async session на запрос), `get_ticket_service`, `get_llm_client`, `get_settings` → в тестах подменяются `app.dependency_overrides`.
- **Транзакции**: граница транзакции — в сервисе (`async with session.begin()`), репозитории не коммитят.
- **Ошибки**: доменные исключения (`TicketNotFound`, `InvalidTransition`) → глобальные exception handlers → единый формат ответа (RFC 7807 `application/problem+json`).
- **Клиенты внешних систем** (`HelpdeskClient`, `DraftLLM`) за интерфейсами (`Protocol`) — легко мокать.
- **App factory** `create_app(settings)` + `lifespan` (engine, httpx-клиент, vectorstore создаются один раз и закрываются корректно).
- Строгая типизация (mypy strict), `Annotated`-зависимости, `StrEnum` для статусов.

```
app/
  main.py                  # create_app(), lifespan, подключение роутеров и handlers
  core/
    config.py              # Settings (pydantic-settings, .env)
    logging.py             # structlog
    exceptions.py          # базовые доменные ошибки + handlers
    db.py                  # engine, async_sessionmaker, Base, get_session
    dependencies.py        # общие Annotated-зависимости
  tickets/                 # домен «обращения»
    router.py              # /api/v1/tickets (JSON)
    webhook.py             # POST /webhooks/helpdesk
    schemas.py             # HelpdeskWebhookIn, TicketOut, ApproveIn, ...
    models.py              # Ticket, TicketStatus
    repository.py          # TicketRepository
    service.py             # TicketService: intake, approve, reject, retry
    exceptions.py
  knowledge/               # домен «база знаний»
    router.py              # /kb/{slug}
    models.py / schemas.py
    retrieval.py           # KnowledgeRetriever: PGVector (text-embedding-3-small) | PG full-text fallback
    ingest.py              # загрузка статей + метаданные (используется seed-скриптом)
  drafting/                # LLM
    chain.py               # LCEL prompt → ChatOpenAI.with_structured_output
    llm.py                 # DraftLLM (реальный / keyless fallback)
    rate_limiter.py        # PG sliding window 60 rpm
    prompts.py, schemas.py # Draft
  helpdesk/
    client.py              # HelpdeskClient (httpx, tenacity, Idempotency-Key)
  jobs/                    # очередь
    models.py              # Job, JobKind, JobStatus
    repository.py          # claim (SKIP LOCKED), complete, fail/backoff
    handlers.py            # generate_draft, send_reply
    worker.py              # цикл воркера, concurrency, LISTEN/NOTIFY
    __main__.py            # python -m app.jobs
  ui/
    router.py              # HTML/HTMX-роуты над теми же сервисами
    templates/
  health/router.py         # GET /health → 200 "OK"
mock_helpdesk/main.py
kb/articles/*.md, kb/embeddings.json, scripts/embed_kb.py, scripts/seed_kb.py, scripts/simulate.py
postman/
  MomentumSupport.postman_collection.json   # Postman v2.1
  local.postman_environment.json            # baseUrl, mockHelpdeskUrl, ticketId
alembic/ (env.py + миграции; CREATE EXTENSION vector)
tests/
Dockerfile (multi-stage, uv, non-root), docker-compose.yml, .env.example
.github/workflows/ci.yml
CLAUDE.md, README.md, docs/architecture.md
pyproject.toml (+ [tool.ruff], [tool.mypy], [tool.pytest])
```

### Postman-коллекция
Папки: **Health**; **Webhook** (новое событие, тот же `event_id` повторно → дубликат, невалидное тело → 422); **Tickets API** (list с фильтром, get, approve с правкой текста, reject, retry, повторный approve → 409); **Knowledge base** (`/kb/{slug}`); **Mock helpdesk** (`GET /replies`, сброс, настройка вероятности 5xx/задержки). Переменные `{{baseUrl}}`, `{{eventId}}` (генерация `{{$guid}}` в pre-request), `{{ticketId}}` сохраняется из ответа. В каждом запросе `pm.test` на статус-код и схему. Запуск без GUI: `npx newman run postman/...json -e postman/local...json` — добавляем как опциональный шаг docker-smoke в CI.

## CI (GitHub Actions, `ci.yml`, на push/PR)
- `lint`: `uv sync --frozen` → `ruff check .` → `ruff format --check .` → `mypy app`.
- `test`: service `pgvector/pgvector:pg16`, `alembic upgrade head`, `pytest --cov` (без ключа OpenAI; задержка keyless-ответа в тестах через `FAKE_LLM_DELAY` = 0, HTTP к OpenAI мокается `respx`).
- `docker`: `docker compose build` + smoke: `compose up -d` → `curl /health` → `compose down`.
- Кэш uv (`astral-sh/setup-uv` с `enable-cache`). Плюс `pre-commit` с ruff локально.
- CD нет (деплой отложен); render/compose-прод — позже.

## Порядок реализации (коммит после каждого шага, без squash)
1. `git init`, uv-проект, ruff/mypy/pytest конфиг, pre-commit, CLAUDE.md, `.gitignore`.
2. Config, логирование, `/health`, Dockerfile, compose (`db`, `api`), CI workflow (lint+test).
3. Модели + Alembic (tickets, jobs, llm_calls, extension vector).
4. Вебхук + intake с дедупом → тесты (дубликаты, конкурентные дубликаты через `asyncio.gather`, время ответа).
5. Очередь/воркер (claim, lease, retry, NOTIFY) → тесты.
6. База знаний: статьи, `embed_kb.py` → `kb/embeddings.json`, сид с метаданными, retrieval (вектор + FTS fallback) → тесты (поиск по предпосчитанным векторам, fallback без ключа).
7. Drafting: LCEL-цепочка, rate limiter, keyless fallback → тесты (лимит, fallback, structured output).
8. Мок хелпдеска + sender + approve/reject → тесты (5xx→retry, timeout, без двойной отправки).
9. UI оператора (Jinja2 + HTMX), страницы KB.
10. Postman-коллекция + environment, newman в CI smoke.
11. `scripts/simulate.py`, README (запуск, архитектура, компромиссы), docs/architecture.md, docker smoke в CI.

## Verification
- `uv run ruff check . && uv run ruff format --check . && uv run mypy app && uv run pytest` — всё зелёное.
- `docker compose up --build` → `curl localhost:8000/health` → `OK`.
- `uv run python scripts/simulate.py --count 500 --duplicates 0.3 --concurrent-dups` → все вебхуки отвечают < 2 с; в БД ровно уникальные `event_id`; черновики появляются, rate ≤ 60/мин (проверка по `llm_calls`).
- Без `OPENAI_API_KEY`: черновики появляются через 5–30 с, в логах видно 401 от OpenAI (embeddings + chat) и fallback (FTS-поиск + статический ответ); ссылки на статьи в черновике есть.
- В UI approve → в моке `GET /replies` ровно один ответ на тикет даже при включённых 5xx/таймаутах мока; двойной клик approve не дублирует.
- Импорт `postman/*.json` в Postman → прогон коллекции зелёный; `newman run` проходит против `docker compose up`.
- Убить `worker` во время генерации → после рестарта job переподхватывается по истёкшему lease.

## Deviations from the plan (found during implementation)

- **Per-kind worker pools.** A load test (500 tickets) showed every worker slot waiting on the LLM rate limiter
  while approved `send_reply` jobs queued behind them. The worker now runs a separate pool per job kind
  (`WORKER_CONCURRENCY` for drafts, `WORKER_SEND_CONCURRENCY` for sends) and routes NOTIFY wakeups per kind.
- **Tests use the compose Postgres** (`support_test` database, `TEST_DATABASE_URL`) instead of testcontainers:
  one less dependency, same real-Postgres guarantees; CI uses a `pgvector/pgvector:pg16` service container.
- **Keyless test uses a local fake OpenAI server.** openai SDK 3.x ships its own HTTP client (`httpx2`) that
  `respx` cannot intercept, so the test starts a tiny asyncio HTTP server answering 401 and asserts the request
  (with the placeholder key) actually arrived.
- **`GET /api/v1/tickets/stats`** added to observe throughput and provider calls per minute during load tests.
- **Rate-limit table is `provider_calls`** (chat + embeddings), not `llm_calls`.
