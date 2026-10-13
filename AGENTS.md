# AGENTS.md

Role-aware log-investigation chatbot: Streamlit UI (`src/ui`) -> FastAPI backend (`src/backend/api.py`) -> LangGraph pipeline (`src/backend/pipeline/graph.py`) -> SereneDB log queries (`src/backend/db`).

## Challenge goal (build target)

One workflow — **question -> evidence -> recommendation** — for three roles: **engineer** (investigate a specific error), **team_lead** (recurring problems/patterns), **manager** (prioritize by impact). Answers stay grounded in log data, use reliable computed summaries, and are tailored per role.

- Role objectives (the tailoring prompts) live in `src/backend/pipeline/roles.py` (`ROLE_OBJECTIVES`); role names in `src/backend/pipeline/state.py` (`RoleName`). Keep these authoritative when changing role behavior.
- "Evidence" = the DB query tools in `src/backend/pipeline/tools/` + `src/backend/db/`; "recommendation" is the answer/confidence stages. Prefer computed aggregates over LLM-inferred numbers.
- Gotcha: `src/ui/roles.py` `ROLES[].description` holds generic assistant copy ("Debug code, review designs…") that does not match `ROLE_OBJECTIVES`. Don't treat it as the source of truth for role intent.

## Setup & commands

- Python 3.14+ and [uv](https://docs.astral.sh/uv/); always run Python through `uv run`, never bare `python`.
- `uv sync` installs deps and the project (editable). `backend` and `ui` are top-level importable from `src/`; run all commands from the repo root (`graph.py` puts its default SQLite checkpointer at `Path.cwd()/.data/checkpoints.sqlite`).
- The shell scripts are NOT executable — run `bash ./backend.sh` / `bash ./frontend.sh`, never `./backend.sh`.
- `bash ./backend.sh` provisions a demo login user (`uv run python -m backend.appdb.demo`) before starting uvicorn `backend.api:app --reload`. `bash ./frontend.sh` runs `streamlit run src/ui/streamlit_app.py`. Ports come from `.env` (`BACKEND_PORT`/`FRONTEND_PORT`; scripts default 8000/8501 when unset).
- Seed the DB (drops/recreates the `log` table, re-embeds, rebuilds the index): `uv run python -m backend.db.setup`. Needs SereneDB on `localhost:7890` (`postgres`/`postgres`, db `postgres`) and Ollama at `host.docker.internal:11434` serving `nomic-embed-text:v1.5`. Re-running discards existing rows.
- Provision a user: `uv run python -m backend.appdb.manage <username> <password>`. `setup.sh` is legacy (`cd src/backend/db && uv run setup.py`) — prefer the module command.
- `LOG_LEVEL=DEBUG bash ./backend.sh` prints tool calls, SQL, and query params.
- `.env` (gitignored) is loaded on import by `src/backend/__init__.py` and `src/ui/__init__.py`; it holds `GROQ_API_KEY`, `DEEPSEEK_API_KEY`, `GOOGLE_API_KEY`, `TYPESAFE_API_KEY`, ports, and auth flags (`AUTH_REGISTRATION_OPEN`, `AUTH_DEMO_*`). `TYPESAFE_API_KEY` is required at import.
- No lint/format/typecheck/CI is configured (none of ruff/black/mypy/pyright installed). Do not invent those commands.

## Testing

- `uv run pytest` (testpaths = `tests`; ~17 modules, nearly all offline).
- Single test: `uv run pytest tests/test_decision_nodes.py::test_scope_routes_general_when_no_clarification_needed`.
- `tests/conftest.py` sets dummy model keys + `CHECKPOINT_BACKEND=memory` before backend import. `test_decision_nodes.py` additionally sets `TYPESAFE_API_KEY` before importing `decision.py`, because that module builds a live `TypeSafeClassifier` at import time.
- Integration: `tests/test_db_queries.py` runs against a live, seeded SereneDB and fails loudly (never skips) if it is unreachable or empty.
- `test_answer_rendering.py` is pure/offline and asserts the serializer and UI renderer stay in sync.

## Pipeline & API notes

- Graph: `normalize -> scope -> (clarify | general | direct_answer | find_evidence) -> answer -> confidence`, with `confidence` looping back to `find_evidence`/`answer`. There is no separate `direct_answer` node — that scope route maps to `answer`. Scope and confidence are TypeSafe classifiers (`decision.py`); thresholds live there (`CLARIFY_THRESHOLD`, `FOLLOW_UP_THRESHOLD`, `MAX_CLARIFY_ROUNDS`) and per-effort budgets in `effort.py`. Model instances are in `models.py` (Groq `gpt-oss-20b`/`120b`, DeepSeek, Gemini).
- `decision.py` builds `TypeSafeClassifier()` at import, so importing `backend.pipeline.decision` (or `backend.api`) needs `TYPESAFE_API_KEY`. `decision.py` uses `effort.py`'s `max_iterations` (the confidence revision-loop cap); the old per-effort confidence-threshold field no longer exists.
- HTTP surface is broader than `/chat/stream`: background investigation jobs (`POST /chats/{id}/investigate`, poll `GET /chats/{id}/run/{run_id}`, plus `active-run`/`cancel`) backed by `backend/jobs.py`, plus `/chat/reframe`, auth, chat CRUD, and `/healthz`/`/readyz`/`/metrics`. `/chat/test` returns canned NDJSON for tests.
- `/chat/stream` emits NDJSON events (`messages`, `tool_results`, `tool_progress`, `subgraph_name`, `subgraph_message`). Answers use `[[RESULT:Rn]]` markers serialized by `pipeline/answers.py` and rendered by `ui/rendering.py`; the serializer and renderer must stay in sync with the `id` fields on tool results.
- DB query functions return `(sql.Composable, params)` and are wrapped by `query_runner`; each query sets `sdb_ai_allow_insecure_endpoint = true`.

## Stale docs to distrust

- README calling `scope`/`confidence` "documented stubs" is outdated — routing is fully implemented (`decision.py`, `graph.py`).
- README lists `src/ui/config.py`; it does not exist — the backend URL is built in `src/ui/chat_client.py`.
- `state.py`'s comment calling `evidence_report_history` "unused today" is stale — `find_evidence` reads and appends it.
