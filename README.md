# ai-chatbot-test

A role-aware **log-investigation chatbot**. Users sign in to a Streamlit UI,
pick a role, and ask questions; a FastAPI + LangGraph backend investigates
against a SereneDB log database and tailors the answer to the role (engineer,
team lead, manager). Accounts and chat history are persisted server-side.

## Architecture

```
Streamlit UI (src/ui)
  │  Authorization: Bearer <token>
  │  POST /auth/login            POST /chats/{id}/investigate  (starts a run)
  │  GET  /chats, /chats/{id}/messages, /chats/{id}/run/{run}  (poll/re-attach)
  ▼
FastAPI backend (src/backend/api.py)
  │                          backend/jobs.py  (background run registry)
  ├──▶ App store (src/backend/appdb)      users · sessions · chats · messages
  │
  ▼
LangGraph pipeline (src/backend/pipeline/graph.py)
  normalize → scope → (clarify | general | direct_answer | find_evidence)
            → answer → confidence   ↺  back to find_evidence / answer
  │
  ▼
SereneDB log queries (src/backend/db)   ← pooled connections
```

The **evidence stage** (`find_evidence`) is a deep agent: it plans the
investigation and calls the database tools itself, then submits a free-text
summary of its findings. The report of `summary` plus `evidence` (and optional
`hypotheses`) is built off-agent from that summary and the captured results,
which are renumbered `R1..Rn` for the answer tables. A bundled
`log-investigation` skill decides when hypotheses apply.

## Prerequisites

- Python 3.14+
- [uv](https://docs.astral.sh/uv/)
- A SereneDB instance on `localhost:7890` (default credentials
  `postgres`/`postgres`, database `postgres`)
- An Ollama server reachable at `host.docker.internal:11434` serving
  `nomic-embed-text:v1.5` for the embedding index

## Environment

Create `.env` in the repo root with the API keys for the providers in use:

```
GROQ_API_KEY=...
DEEPSEEK_API_KEY=...

# Optional ports (defaults: 8000 backend, 8501 UI)
BACKEND_PORT=8000
FRONTEND_PORT=8501
```

Keys and ports are loaded automatically on import via `python-dotenv`. The UI
reads `BACKEND_PORT` to reach the backend, so both scripts stay in sync.

### Concurrency tuning (optional)

The backend serves many users at once; the defaults below are sized for roughly
a few dozen concurrent investigations.

| Variable | Default | Purpose |
| --- | --- | --- |
| `MAX_THREADS` | `100` | anyio worker-pool ceiling; bounds in-flight streams |
| `MAX_GLOBAL_INFLIGHT` | `32` | Server-wide investigations; excess requests get `503` |
| `MAX_INFLIGHT_PER_USER` | `2` | Per-user investigations; excess get `429` |
| `SERENEDB_POOL_MIN` / `SERENEDB_POOL_MAX` | `2` / `32` | Shared SereneDB connection pool |
| `SERENEDB_STATEMENT_TIMEOUT_MS` | `30000` | Server-side per-query timeout |
| `SERENEDB_HOST` / `SERENEDB_PORT` / `SERENEDB_DB` / `SERENEDB_USER` / `SERENEDB_PASSWORD` | `localhost` / `7890` / `postgres` / `postgres` / `postgres` | SereneDB target |
| `CHECKPOINT_BACKEND` | `sqlite` | Graph state store: `sqlite`, `memory`, or `postgres` |
| `CHECKPOINT_SQLITE_PATH` | `./.data/checkpoints.sqlite` | SQLite checkpoint file |
| `CHECKPOINT_POSTGRES_URL` | – | Required when `CHECKPOINT_BACKEND=postgres` |
| `MODEL_MAX_RETRIES` / `MODEL_RETRY_BASE_DELAY` / `MODEL_RETRY_MAX_DELAY` | `3` / `1.0` / `20.0` | Retry/backoff for rate-limit and connection errors |
| `APP_DB_PATH` | `./.data/app.sqlite` | Users, sessions, chats, and messages |
| `SESSION_TTL_HOURS` | `168` | Bearer-session lifetime |
| `AUTH_REGISTRATION_OPEN` | `false` | Allow self-service sign-up via `/auth/register` |

## Concurrency

The pipeline is blocking (sync LangGraph nodes, sync model clients, sync
psycopg2). The API therefore exposes **sync** endpoints (`def`, not `async def`)
so FastAPI/Starlette run each investigation on the anyio worker pool instead of
the event loop; users are then served in parallel rather than one at a time.

## Background investigations

Generation is **decoupled from the client that starts it**. `POST
/chats/{id}/investigate` persists the user turn, launches the pipeline on a
worker thread (`backend/jobs.py`), and returns a `run_id` immediately; the
worker buffers every event and, on completion, persists the assistant message
itself. The UI is only a viewer:

- `GET /chats/{id}/run/{run_id}?since=N[&wait=S]` replays buffered events from a
  cursor and reports `running | done | error | cancelled` (the `wait` parameter
  long-polls).
- `GET /chats/{id}/active-run` lets a fresh session re-attach to an in-flight run.
- `POST /chats/{id}/run/{run_id}/cancel` (and `DELETE /chats/{id}`) stop a run.

Because the run outlives any single HTTP request or Streamlit script run,
switching conversations, deleting a chat, or refreshing the browser no longer
truncates a half-rendered answer or erases its thinking — the work continues and
the result still lands in the chat store. Runs are held in-process (single
uvicorn worker); the retry/backoff, connection-pool, checkpointer, and admission
controls below are unchanged. `POST /chat/stream` remains available as a direct,
request-scoped NDJSON stream for compatibility.

Supporting pieces:

- **Connection pool** — `backend/db/connection.py` borrows from a shared
  `ThreadedConnectionPool`; no connection-per-query.
- **Checkpointer** — `graph.py` uses a durable `SqliteSaver` by default (thread
  safe, single node). Set `CHECKPOINT_BACKEND=postgres` to share state across
  worker processes/replicas.
- **Admission control** — global and per-user in-flight caps reject excess load
  with `503`/`429` and `Retry-After`, protecting the shared provider keys.
- **Retry/backoff** — model calls classified as rate-limit/connection failures
  are retried with exponential backoff before surfacing an error.

> Chat histories are now stored server-side per authenticated user, so they
> survive refresh and restart. See **Authentication** below.

## Authentication

Accounts, sessions, and chats are owned by the backend (`backend/appdb`), and the
Streamlit UI is a thin client that holds a bearer token.

- Passwords are hashed with scrypt (stdlib only); only a SHA-256 digest of each
  session token is stored.
- `POST /auth/register`, `/auth/login`, `/auth/logout`, and `GET /auth/me`
  manage accounts and sessions. Registration is closed by default; set
  `AUTH_REGISTRATION_OPEN=true` to allow self-service sign-up, or provision
  users with:
  ```bash
  uv run python -m backend.appdb.manage <username> <password>
  ```
- Every `/chat/*` and `/chats*` endpoint requires `Authorization: Bearer <token>`;
  the caller's `user_id` is taken from the token, never from the request body.

### Demo auto-login (temporary)

The demo auto-login is isolated so it can be deleted later. It lives in exactly
two modules, `src/ui/login/demo.py` (UI flag + credentials + auto sign-in) and
`src/backend/appdb/demo.py` (provisions the account), and is controlled by
`AUTH_DEMO_AUTOLOGIN` (**default ON**). Set `AUTH_DEMO_AUTOLOGIN=false` to skip
the demo and see the real sign-in page.

When the demo is active, the auth gate auto-signs in as the demo account before
the chat UI renders; the sidebar then lists that account's chats and loads each
chat's history on demand. `./backend.sh` provisions the demo account on start;
`./frontend.sh` alone does not, so the UI will not boot until the account exists.

To remove the demo:

1. Delete `src/ui/login/demo.py` and `src/backend/appdb/demo.py`.
2. Delete `tests/test_demo.py`.
3. Delete the demo provisioning comment and the
   `uv run python -m backend.appdb.demo` line from `backend.sh`.
4. In `src/ui/streamlit_app.py`: delete the `from ui.login import demo` import,
   delete the demo comment lines above the gate, delete the
   `demo.mark_logged_out()` line from `_logout()`, and reduce the gate to
   `login_page(); st.stop()`.
5. Delete the "Demo auto-login (temporary)" section from `src/ui/login/README.md`
   (the file itself stays — it also documents the permanent auth flow).
6. Delete the "Demo auto-login" section from `README.md`.

Once the demo is gone, `login_page()` is the only way in, so you need either
`AUTH_REGISTRATION_OPEN=true` (to create an account from the UI) or an account
provisioned ahead of time with:

```bash
uv run python -m backend.appdb.manage <username> <password>
```

## Setup

```bash
uv sync                       # install dependencies and the project itself
uv run python -m backend.db.setup   # create the AI secret and import the sample log CSV
```

`backend.db.setup` drops and recreates the `log` table from
`src/backend/data/OpenStack_2k.log_structured.csv`, generates embeddings, and
builds the inverted index. It is safe to re-run, but it discards existing rows.

## Running

```bash
./backend.sh     # uvicorn backend.api:app --port $BACKEND_PORT --reload
./frontend.sh    # streamlit run src/ui/streamlit_app.py --server.port $FRONTEND_PORT
```

Both commands run from the repository root. Set `BACKEND_PORT`/`FRONTEND_PORT`
in `.env` (or the environment) to run on non-default ports; `8000`/`8501` are
used when unset.

## Testing

```bash
uv run pytest
```

The suite in `tests/test_db_queries.py` runs the database query layer against a
**live SereneDB**. It requires the DB to be running on `localhost:7890` and
seeded (see Setup); if SereneDB is unreachable or empty the tests **fail
loudly** rather than skip.

Everything else runs without external services:

| File | Covers |
| --- | --- |
| `test_auth.py` | scrypt hashing, users, sessions, expiry, bearer dependency |
| `test_chats.py` | chat/message CRUD, ordering, upsert, cascade delete, isolation |
| `test_api.py` | auth gating, chat endpoints, NDJSON streaming, admission limits, request ids, metrics |
| `test_jobs.py` | background run registry and investigation endpoints (buffer, re-attach, cancel, persistence) |
| `test_middleware.py` | error classification and retry/backoff |
| `test_checkpointer.py` | checkpointer backend selection |
| `test_config.py` / `test_connection.py` | env-driven config and the pooled connection layer |
| `test_observability.py` | metrics registry and request-correlation middleware |
| `test_chat_client.py` / `test_rendering.py` / `test_manage.py` | UI client, marker rendering, provisioning CLI |

To see tool calls and their parameters (and, at `DEBUG`, the SQL and query
params) in the backend terminal:

```bash
LOG_LEVEL=DEBUG ./backend.sh
```

## HTTP API

All `/chat/*` and `/chats*` endpoints require `Authorization: Bearer <token>`.

### Auth

| Endpoint | Body / result |
| --- | --- |
| `POST /auth/register` | `{username, password}` → `{token, expires_at, user}` |
| `POST /auth/login` | `{username, password}` → `{token, expires_at, user}` |
| `POST /auth/logout` | revokes the bearer token → `{ok: true}` |
| `GET /auth/me` | current user → `{id, username}` |

### `POST /chat/stream`

> Compatibility only. The UI now uses the background-investigation endpoints
> below, which survive a disconnected client. `/chat/stream` still returns a
> request-scoped `StreamingResponse` of the same NDJSON events.

Request body (the user is derived from the bearer token):

```json
{
  "chat_id": "20250101120000",
  "role": "engineer",
  "effort": "Medium",
  "history": [{"role": "user", "content": "..."}]
}
```

Streams newline-delimited JSON events, each `{"type", "content"}`:

| type              | content                                                  |
| ----------------- | -------------------------------------------------------- |
| `messages`        | partial/final answer text                                |
| `tool_results`    | JSON list of tool-result records (`id`, `tool_name`, ...) |
| `tool_progress`   | JSON progress event (`operation`, `status`)              |
| `subgraph_name`   | current pipeline stage name                              |
| `subgraph_message`| reasoning text from a subgraph                           |

Assistants return structured prose/table blocks; the backend serializes them into
`[[RESULT:R1]]` markers (alone on their own line) and the UI replaces each marker
with the corresponding table from `tool_results`.

### `POST /chat/reframe`

Re-explain a completed investigation for another role (one-shot, stateless).
Body: `target_role`, `effort`, `question`, `previous_answer`, `tool_results`.
Returns `{"answer": "..."}`.

### `POST /chat/test`

Static event stream used to exercise the UI without running the pipeline.

### Chats

| Endpoint | Result |
| --- | --- |
| `GET /chats` | `{chats: [{id, role, title, created_at, updated_at}]}` |
| `POST /chats` | create `{chat_id, role, title?}` → chat record |
| `DELETE /chats/{chat_id}` | `{deleted: bool}` |
| `GET /chats/{chat_id}/messages` | `{messages: [...]}` |
| `POST /chats/{chat_id}/messages` | append `{role, content, tool_results?, progress?, reframed_for?}` |

### Background investigations

| Endpoint | Result |
| --- | --- |
| `POST /chats/{chat_id}/investigate` | persist the user turn and start a run: `{role, effort, question, history}` → `{run_id, status}` |
| `GET /chats/{chat_id}/run/{run_id}` | `?since=N&wait=S` → `{status, events, next_since, error, result}` |
| `GET /chats/{chat_id}/active-run` | `{run_id}` for an in-flight run, else `{run_id: null}` |
| `POST /chats/{chat_id}/run/{run_id}/cancel` | `{cancelled: bool}` |

`status` is `running | done | error | cancelled`. On `done`, `result` carries
`{answer, tool_results, progress}` — the same payload the backend persists as
the assistant message. `wait` long-polls (capped at 30s). Starting an
investigation for a chat that already has one returns the existing `run_id`.
`DELETE /chats/{chat_id}` cancels and discards any active run.

### Operations

| Endpoint | Result |
| --- | --- |
| `GET /healthz` | liveness → `{status: "ok"}` |
| `GET /readyz` | readiness; checks the app store and SereneDB → `200`/`503` |
| `GET /metrics` | in-process counters + admission-control gauges (JSON) |

Every response carries an `X-Request-ID` (generated, or echoed from the request)
and each request is logged as `METHOD path -> status (ms) [rid=...]`. Counters
include streams started/completed/rejected, logins, chats, and messages.

## Project layout

```
src/
├── backend/
│   ├── api.py            FastAPI endpoints, streaming, and admission control
│   ├── jobs.py           in-process background investigation registry
│   ├── observability.py  request-correlation middleware and metrics registry
│   ├── appdb/            users, sessions, chats, messages (SQLite)
│   ├── db/               pooled SereneDB connection, config, and log queries
│   └── pipeline/
│       ├── graph.py      LangGraph nodes, wiring, and checkpointer selection
│       ├── state.py      pipeline state schema
│       ├── contracts.py  Pydantic agent response models
│       ├── answers.py    structured-answer → marker text serialization
│       ├── roles.py      role objectives
│       ├── effort.py     per-effort budgets
│       ├── middleware.py model/tool error handling and retry/backoff
│       ├── models.py     chat model instances
│       ├── prompts/      prompt builders + static system prompts
│       ├── agents/       one module per LLM agent, incl. the `find_evidence`
│       │                 deep agent and its off-agent report builder
│       └── tools/        database tools, executor, result collection
└── ui/
    ├── streamlit_app.py  entry point, auth gate, and sidebar
    ├── chat_client.py    backend HTTP client (auth, chats, streaming) + base URL
    ├── rendering.py      answer/marker rendering
    ├── roles.py          role labels and cards
    ├── login/            sign-in / registration page
    ├── main_page/        active chat page
    └── new_chat/         role-selection page
skills/
└── log-investigation/SKILL.md  hypothesis workflow for the evidence stage
```
