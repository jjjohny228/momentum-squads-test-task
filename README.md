# Momentum Support — LLM-чернетки відповідей для хелпдеску

Сервіс приймає звернення від хелпдеску через вебхук, знаходить релевантні статті в базі знань (RAG на PGVector),
генерує чернетку відповіді з посиланнями (LangChain + OpenAI), показує її оператору в веб-інтерфейсі й після
підтвердження надсилає клієнту через API хелпдеску рівно один раз.

**Стек:** Python 3.14 · FastAPI · SQLAlchemy 2 (async, psycopg 3) · Alembic · PostgreSQL 16 + pgvector ·
LangChain (`langchain-openai`, `langchain-postgres`) · Jinja2 + HTMX · uv · ruff · mypy (strict) · pytest ·
Docker Compose · GitHub Actions.

## Швидкий старт

Потрібні лише Docker і Docker Compose.

```bash
git clone <repo> && cd MomentumTestTask
cp .env.example .env
docker compose up --build
```

У `docker-compose.yml` немає жодного захардкодженого секрету. Креди БД беруться з `.env`, а без `POSTGRES_*`
compose не стартує. Порт Postgres назовні не публікується: БД доступна тільки всередині мережі compose
(`docker compose exec db psql -U "$POSTGRES_USER" "$POSTGRES_DB"`). Мок хелпдеску вмикається профілем
`COMPOSE_PROFILES=mock` (вже прописано в `.env.example`).

Для тестів і CI є окремий ізольований стек `docker-compose.test.yml`: власне ім'я проєкту, volume і порт
(`localhost:5434`), одноразові креди `test/test`, прискорений keyless-режим.

| Що | Адреса |
|---|---|
| Інтерфейс оператора | http://localhost:8000/ |
| Swagger / OpenAPI | http://localhost:8000/docs |
| Вебхук | `POST http://localhost:8000/webhooks/helpdesk` |
| Health | http://localhost:8000/health → `OK` |
| Мок хелпдеску (Swagger) | http://localhost:8001/docs |

Сервіс `migrate` запускається першим: застосовує міграції Alembic і завантажує базу знань (`kb/articles`) в БД.
Після нього стартують `api`, `worker` і `mock-helpdesk`.

### Спробувати

```bash
# 1. Хелпдеск надсилає звернення (мок сам формує payload, шле з таймаутом 2 с і дублями)
curl -X POST localhost:8001/simulate/tickets -H 'content-type: application/json' \
     -d '{"message": "Не можу експортувати звіт у PDF", "concurrent_duplicates": 3}'

# 2. Через 5–30 с чернетка з'явиться в UI: http://localhost:8000/ — Approve / Reject

# 3. Що отримав «клієнт» (рівно одну відповідь на тікет, навіть при збоях мока)
curl localhost:8001/replies
```

Або напряму у вебхук:

```bash
curl -X POST localhost:8000/webhooks/helpdesk -H 'content-type: application/json' -d '{
  "event_id": "evt_8f2a",
  "ticket_id": "T-1042",
  "customer_email": "user@example.com",
  "message": "Не можу експортувати звіт у PDF",
  "reply_url": "http://mock-helpdesk:8001/tickets/T-1042/replies"
}'
```

`reply_url` має вказувати на дозволений хост (`REPLY_URL_ALLOWED_HOSTS`, у compose — `mock-helpdesk`).

### Postman

Імпортуйте `postman/MomentumSupport.postman_collection.json` і середовище `postman/local.postman_environment.json`.
Запуск усієї колекції (Runner) проходить наскрізний сценарій: вебхук → дубль → очікування чернетки → approve
(з редагуванням) → повторний approve = 409 → очікування відправки → у моку рівно одна відповідь.
Без GUI:

```bash
npx newman run postman/MomentumSupport.postman_collection.json -e postman/local.postman_environment.json
```

### Навантаження

```bash
uv run python -m scripts.simulate --count 500 --duration 600 --duplicates 0.3 --wait
```

Скрипт поводиться як хелпдеск (таймаут 2 с, повтори, частина подій — дублі, у т.ч. одночасні), друкує latency
вебхука і стежить через `/api/v1/tickets/stats`, щоб кількість запитів до провайдера за 60 с не перевищувала ліміт.
Приклад локального прогону (500 звернень за 30 с, тобто у 20 разів щільніше за пік із ТЗ): 808 HTTP-доставок,
p50 = 11 мс, p95 = 26 мс, max = 100 мс, створено рівно 500 тікетів, 308 дублів відсічено, у будь-якому 60-секундному
вікні — не більше 60 запитів до OpenAI.

## Режим без API-ключа

Якщо `OPENAI_API_KEY` порожній, сервіс працює повністю:

- **Генерація.** Запит до OpenAI все одно відправляється (з ключем-заглушкою; отримує 401). Замість відповіді
  моделі використовується статична чернетка з посиланнями на знайдені статті. Вона повертається з випадковою
  затримкою 5–30 с, як справжній виклик. Далі по пайплайну вона йде як звичайна (`draft_source = fallback`).
- **Пошук.** Ембединг запиту теж робиться реальним запитом (401). Після нього пошук перемикається на повнотекстовий
  пошук Postgres за тими самими статтями.
- **Ліміт.** Обидва запити рахуються в ліміті 60 rpm.

> Зверніть увагу: у keyless-режимі текст звернення фізично надсилається на api.openai.com (і відхиляється на етапі
> автентифікації). Так вимагає ТЗ; для продакшну без ключа це варто вимкнути.

З ключем: `gpt-5.4-mini` (`reasoning_effort=low`), `text-embedding-3-small`. Обидві моделі задаються через env.

### Ембединги бази знань

Вектори статей кешуються в `kb/embeddings.json` (за id та хешем чанка), тож сід не витрачає запити повторно.
Щоб оновити кеш після зміни статей:

```bash
OPENAI_API_KEY=sk-... uv run python -m scripts.embed_kb
```

Якщо ключ є, а кешу немає, `migrate` порахує ембединги сам. Якщо немає ні ключа, ні кешу, працює повнотекстовий
пошук.

## Трейсинг у LangSmith (threads)

Увімкніть у `.env` і перезапустіть стек:

```bash
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=lsv2_...
LANGSMITH_PROJECT=momentum-support
# LANGSMITH_ENDPOINT=https://eu.api.smith.langchain.com   # якщо акаунт в EU
```

**Один тікет — один thread.** Кожен трейс тікета має metadata `thread_id = ticket_id` із хелпдеску (`T-1042`), тож
у LangSmith (**Threads** у проєкті `momentum-support`) видно весь життєвий цикл звернення. Наступні події того
самого тікета хелпдеску потрапляють у той самий тред.

| Root-трейс | Звідки | Що всередині |
|---|---|---|
| `generate_draft` | worker | `kb_retrieve` (retriever: стратегія vector/fulltext, знайдені статті зі score) → `draft_chain` (prompt → `ChatOpenAI` → structured output, токени, latency) або `keyless_static_draft` з помилкою 401 від провайдера |
| `operator_approve` / `operator_reject` / `operator_retry` | api | чернетка, чи редагував оператор, фінальний текст |
| `send_reply` | worker | `reply_url`, текст, `idempotency_key`; кожна спроба (ретраї після 5xx/таймаутів) — окремий трейс з помилкою |

Metadata кожного рану: `thread_id`, `ticket_uuid`, `event_id`, `job_id`, `attempt`. За ними можна фільтрувати,
наприклад `metadata.attempt > 1`, щоб знайти ретраї. У картці тікета в UI показано його `thread_id`.
Без `LANGSMITH_API_KEY` трейсинг вимкнений і нічого нікуди не відправляється.

## Архітектура

```
Helpdesk ──POST /webhooks/helpdesk──▶ api ── 1 транзакція: INSERT ticket ON CONFLICT(event_id) DO NOTHING
                                       │                     + INSERT job(generate_draft) + NOTIFY
                                       └─▶ 202 за мілісекунди (дубль → 200)

worker ── пул generate_draft (30 слотів) ─▶ retrieve (PGVector | FTS) ─▶ rate limiter ─▶ LLM ─▶ draft_ready
       └─ пул send_reply (10 слотів) ─────▶ POST reply_url + Idempotency-Key ─▶ sent

operator UI / JSON API ── approve: UPDATE … WHERE status='draft_ready' + job(send_reply) в одній транзакції
```

Статуси тікета: `received → generating → draft_ready → approved → sending → sent`, а також `rejected` і `failed`
(з кнопкою «Повторити»).

### Як закрито вимоги ТЗ

| Вимога | Рішення |
|---|---|
| Вебхук < 2 с | У запиті тільки валідація та одна транзакція з двома INSERT. LLM викликається тільки у воркері. |
| Дублі, у т.ч. одночасні | `UNIQUE(event_id)` + `INSERT … ON CONFLICT DO NOTHING RETURNING id`. Гонку розв'язує Postgres; задача створюється лише для реально вставленого рядка. |
| Не губити звернення (пік 500/10 хв) | Звернення та задача пишуться атомарно в Postgres ще до відповіді 202. Черга на `SELECT … FOR UPDATE SKIP LOCKED` з lease: якщо воркер впав, задачу підхоплює інший після закінчення lease. Heartbeat подовжує lease довгим задачам. |
| LLM 5–30 с, 60 rpm на весь сервіс | asyncio-пул на 30 слотів. Ковзне вікно в Postgres (`provider_calls` + `pg_advisory_xact_lock`) спільне для всіх реплік воркера. Через ліміт проходить кожен запит до провайдера: чат і ембединги, ретраї, keyless-режим. Вбудовані ретраї SDK вимкнені, щоб жоден запит не обходив ліміт. |
| Хелпдеск дає 5xx / таймаут | Ретраї з експоненційною затримкою та jitter. 4xx — постійна помилка, тікет переходить у `failed`, оператор бачить кнопку «Повторити». |
| Клієнт не отримує відповідь двічі | (1) approve — compare-and-set `UPDATE … WHERE status='draft_ready'`, тож подвійний клік чи дві вкладки дають лише одну задачу; (2) перед відправкою — CAS `approved → sending`, після 201 — `sent`; (3) кожна спроба йде з тим самим `Idempotency-Key: reply-<id>`. |
| Мок хелпдеску | `mock_helpdesk/`: випадкові 503 і «повільні» відповіді (відповідь збережена, але клієнт не дочекався), дедуп за `Idempotency-Key`, `GET /replies` зі статистикою. Також вміє слати вебхуки як справжній хелпдеск. |

**Чесне застереження щодо exactly-once.** Якщо запит відправки обірвався за таймаутом, ми не знаємо, чи дійшов він.
Тому повтор без дедуплікації на боці отримувача може продублювати відповідь, і це принципове обмеження мережі,
а не цього коду. Сервіс надсилає стабільний `Idempotency-Key`, мок його враховує. Від реального API хелпдеску
потрібна та сама гарантія (або перевірка «чи вже є така відповідь» перед повтором).

### Свідомі компроміси

- **Черга в Postgres, а не Celery/RabbitMQ/Redis.** Задача пишеться в тій самій транзакції, що й тікет, тож
  outbox не потрібен. Навантаження (~50 задач/хв) для Postgres мізерне. Celery синхронний, а його `rate_limit`
  діє на кожен воркер окремо, тож глобальний ліміт усе одно довелося б писати самим. Якщо навантаження зросте
  на порядки, можна перейти на Taskiq/arq + Redis з outbox-relay; інтерфейс `JobRepository` для цього вже ізольований.
- **Ембединги рахуються в той самий ліміт** (`RATE_LIMIT_INCLUDE_EMBEDDINGS=true`): по 2 запити на звернення,
  тобто ~30 звернень/хв. Пік 500/10 хв розбирається з черги приблизно за 17 хв без втрат. Якщо ТЗ мало на увазі
  лише чат-запити, прапорець `false` подвоює пропускну здатність.
- **Окремі пули для генерації та відправки.** Без цього беклог чернеток, що чекають на ліміт, блокував би відправку
  вже підтверджених відповідей. Цю проблему знайшли під час навантажувального тесту.
- **Посилання на статті додає код, а не модель.** LLM повертає лише `cited_slugs` (structured output). URL беруться
  з метаданих чанків, тому вигадати посилання модель не може.
- **Повнотекстовий fallback** використовує конфіг `simple` та префікси слів: Postgres не має словника української,
  а для 8 статей цього достатньо.

## Розробка

```bash
uv sync                                  # залежності (Python 3.14)
docker compose -f docker-compose.test.yml up -d db   # тестовий Postgres: localhost:5434, test/test
export DATABASE_URL=postgresql+psycopg://test:test@localhost:5434/support
uv run alembic upgrade head
uv run python -m scripts.seed_kb
uv run uvicorn app.main:app --reload     # API + UI
uv run python -m app.jobs                # воркер
uv run uvicorn mock_helpdesk.main:app --port 8001
```

Перевірки (ті самі, що в CI):

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
```

Тести йдуть на справжньому Postgres (`support_test` з тестового стеку; інший можна задати через `TEST_DATABASE_URL`), бо `SKIP LOCKED`, `ON CONFLICT` і advisory locks не
мокаються. Покрито: дедуп вебхука (у т.ч. 20 одночасних доставок), швидкість під сплеском, повний життєвий цикл
тікета, подвійний approve, ретраї відправлення з тим самим ключем, вичерпання спроб, lease після «падіння» воркера,
ліміт на двох інстансах лімітера, ізоляцію пулів, векторний і FTS-пошук, метадані чанків, keyless-режим із
реальним HTTP-запитом до фейкового OpenAI, класифікацію помилок хелпдеску, UI.

### CI (GitHub Actions)

`.github/workflows/ci.yml`:
1. **lint**: `ruff check`, `ruff format --check`, `mypy` (strict);
2. **test**: pytest із сервісом `pgvector/pgvector:pg16` і coverage;
3. **docker**: `docker compose -f docker-compose.test.yml up --build`, перевірка `/health`, прогін Postman-колекції через newman проти
   живого стеку (зі збоями мока).

Деплою немає, CD поки не налаштовано.

### Структура

```
app/
  core/        config, db, logging, exceptions (RFC 7807), DI-залежності
  tickets/     webhook, JSON API, schemas, models, repository, service (статуси, транзакції)
  knowledge/   моделі статей, ingest (чанки + метадані), retrieval (PGVector | FTS), сторінки /kb
  drafting/    LCEL-ланцюжок, промпти, keyless fallback, rate limiter, ретраї провайдера
  helpdesk/    HTTP-клієнт хелпдеску (Idempotency-Key, класифікація помилок)
  jobs/        черга (claim/lease/backoff), обробники, воркер, точка входу `python -m app.jobs`
  ui/          Jinja2 + HTMX над тим самим TicketService
mock_helpdesk/ мок API хелпдеску + симулятор вебхуків
kb/            статті бази знань (markdown + front matter), кеш ембедингів
scripts/       seed_kb, embed_kb, simulate
postman/       колекція та середовище
alembic/       міграції
tests/
```

Кожен домен має шари `router → service → repository → models`. Pydantic-схеми — контракт API; ORM-моделі назовні
не віддаються. Межа транзакції проходить у сервісі.

## Конфігурація

Усе через змінні середовища (див. `app/core/config.py`, `.env.example`). Основне:

| Змінна | За замовчуванням | |
|---|---|---|
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | — (обов'язкові) | або повний `DATABASE_URL`; поза Docker також `POSTGRES_HOST` / `POSTGRES_PORT` |
| `OPENAI_API_KEY` | — | порожній → keyless-режим |
| `OPENAI_MODEL` / `OPENAI_EMBEDDING_MODEL` | `gpt-5.4-mini` / `text-embedding-3-small` | |
| `RATE_LIMIT_RPM` | `60` | спільний бюджет усіх воркерів |
| `RATE_LIMIT_INCLUDE_EMBEDDINGS` | `true` | рахувати ембединги в ліміт |
| `WORKER_CONCURRENCY` / `WORKER_SEND_CONCURRENCY` | `30` / `10` | розміри пулів |
| `JOB_MAX_ATTEMPTS`, `JOB_LEASE_SECONDS` | `6`, `120` | |
| `REPLY_URL_ALLOWED_HOSTS` | `[]` (усі) | захист від SSRF через `reply_url` |
| `WEBHOOK_SECRET` | — | якщо задано, вимагає заголовок `X-Webhook-Secret` |
| `OPERATOR_USERNAME` / `OPERATOR_PASSWORD` | — | HTTP Basic для UI та `/api/v1` |
| `LANGSMITH_TRACING` / `LANGSMITH_API_KEY` / `LANGSMITH_PROJECT` | `false` / — / `momentum-support` | трейсинг, thread = тікет |
| `MOCK_FAILURE_RATE` / `MOCK_SLOW_RATE` / `MOCK_SLOW_SECONDS` | `0.2` / `0.1` / `12` | хаос у моку |

## Робота з AI-агентом

Проєкт зроблено з Claude Code. Інструкції для агента лежать у `CLAUDE.md`, план і рішення — у
`docs/architecture.md`. Історія комітів залишена як є.
