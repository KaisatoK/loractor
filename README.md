# ai-chatbot-test

A role-aware **log-investigation chatbot**. A Streamlit UI collects a question and a
user role, a FastAPI + LangGraph backend investigates it against a SereneDB log
database, and the answer is tailored to the role (engineer, team lead, manager).

## Architecture

```
Streamlit UI (src/ui)
  │  POST /chat/stream   (NDJSON event stream)
  ▼
FastAPI backend (src/backend/api.py)
  │
  ▼
LangGraph pipeline (src/backend/pipeline/graph.py)
  normalize → scope → (general | find_evidence → answer → confidence)
  │
  ▼
SereneDB log queries (src/backend/db)
```

The LLM stages use different providers: Groq (`gpt-oss-20b` for normalization and
the general agent, `gpt-oss-120b` for the final answer), DeepSeek (evidence
agent), and Gemini (`evidence_report` formatter).

> `scope` and `confidence` are currently documented stubs: every request is
> treated as `unresolved` and runs the evidence path, and the answer is always
> accepted. The routing code and prompt builders are retained for re-enabling
> TypeSafe classification later.

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
GOOGLE_API_KEY=...

# Optional ports (defaults: 8000 backend, 8501 UI)
BACKEND_PORT=8000
FRONTEND_PORT=8501
```

Keys and ports are loaded automatically on import via `python-dotenv`. The UI
reads `BACKEND_PORT` to reach the backend, so both scripts stay in sync.

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

To see tool calls and their parameters (and, at `DEBUG`, the SQL and query
params) in the backend terminal:

```bash
LOG_LEVEL=DEBUG ./backend.sh
```

## HTTP API

### `POST /chat/stream`

Request body:

```json
{
  "user_id": "uuid",
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

## Project layout

```
src/
├── backend/
│   ├── api.py            FastAPI endpoints and the streaming protocol
│   ├── db/               connection, config, and log queries
│   └── pipeline/
│       ├── graph.py      LangGraph nodes and wiring
│       ├── state.py      pipeline state schema
│       ├── contracts.py  Pydantic agent response models
│       ├── answers.py    structured-answer → marker text serialization
│       ├── roles.py      role objectives
│       ├── effort.py     per-effort budgets
│       ├── middleware.py model/tool error handling
│       ├── models.py     chat model instances
│       ├── prompts/      prompt builders + static system prompts
│       ├── agents/       one module per LLM agent
│       └── tools/        database tools, executor, result collection
└── ui/
    ├── streamlit_app.py  entry point and sidebar
    ├── chat_client.py    backend HTTP client
    ├── rendering.py      answer/marker rendering
    ├── roles.py          role labels and cards
    ├── main_page/        active chat page
    └── new_chat/         role-selection page
```
