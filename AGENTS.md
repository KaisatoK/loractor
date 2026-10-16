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
- The shell scripts are NOT executable — run `bash ./backend.sh` / `bash ./frontend.sh` / `bash ./routes.sh`, never `./<name>.sh`.
- `bash ./backend.sh` provisions a demo login user (`uv run python -m backend.appdb.demo`) before starting uvicorn `backend.api:app --reload`. `bash ./frontend.sh` runs `streamlit run src/ui/streamlit_app.py`. Ports come from `.env` (`BACKEND_PORT`/`FRONTEND_PORT`; scripts default 8000/8501 when unset).
- Seed the DB (drops/recreates the `log` table, re-embeds, rebuilds the index): `uv run python -m backend.db.setup`. Needs SereneDB on `localhost:7890` (`postgres`/`postgres`, db `postgres`) and Ollama at `host.docker.internal:11434` serving `nomic-embed-text:v1.5`. Re-running discards existing rows.
- Provision a user: `uv run python -m backend.appdb.manage <username> <password>`. `setup.sh` is legacy (`cd src/backend/db && uv run setup.py`) — prefer the module command.
- `LOG_LEVEL=DEBUG bash ./backend.sh` prints tool calls, SQL, and query params.
- `.env` (gitignored) is loaded on import by `src/backend/__init__.py` and `src/ui/__init__.py`; it holds `GROQ_API_KEY`, `DEEPSEEK_API_KEY`, `GOOGLE_API_KEY`, `TYPESAFE_API_KEY`, ports, and auth flags (`AUTH_REGISTRATION_OPEN`, `AUTH_DEMO_*`). `TYPESAFE_API_KEY` is required at import — as are the three model keys, because `models.py` builds its chat clients at import time.
- No lint/format/typecheck/CI is configured (none of ruff/black/mypy/pyright installed). Do not invent those commands.
- `requirements.txt` is **generated**, not hand-edited — it is `uv export` output (the header names the command). Change dependencies in `pyproject.toml` and re-export.
- Line endings: `.gitattributes` sets `* text=auto` and `*.sh eol=lf`. Keep new/edited files LF; untracked files are not normalized by git, so a stray CRLF write persists.
- When scripting against the app, set `CHECKPOINT_BACKEND=memory` (and point `APP_DB_PATH` outside the repo if relevant) so you do not create or mutate `.data/`.

## Testing

- `uv run pytest` (testpaths = `tests`; 20 modules, nearly all offline).
- Single test: `uv run pytest tests/test_decision_nodes.py::test_scope_routes_route_when_queryable_scope_present`.
- `tests/conftest.py` sets dummy `GROQ_API_KEY` / `DEEPSEEK_API_KEY` / `GOOGLE_API_KEY` + `CHECKPOINT_BACKEND=memory` before backend import. `test_decision_nodes.py` additionally sets `TYPESAFE_API_KEY` before importing `decision.py`, because that module builds a live `TypeSafeClassifier` at import time.
- Integration: `tests/test_db_queries.py` runs against a live, seeded SereneDB and fails loudly (never skips) if it is unreachable or empty.
- Live route tests (`tests/test_live_scope_routes.py`): skipped offline; run alone with `bash ./routes.sh` (sets `RUN_LIVE_ROUTES=1`) — real normalize + scope-classifier calls, ~2 per asserted turn. Must not run inside the full suite (conftest installs dummy keys before the backend's import-time clients; the module reloads `.env` with `override=True` only when gated). Gated runs write the HTML report `.data/scope_routes_report.html` (gitignored; override `RUN_LIVE_ROUTES_REPORT`; its path is printed once per run), and the per-turn numbers appear in an assertion failure. Routes are stochastic — a flipped row shows as `✗` with raw numbers; re-run before treating it as a regression.
- `test_answer_rendering.py` is pure/offline and asserts the serializer and UI renderer stay in sync.

### Live route cases are coupled to normalize/scope

`tests/test_live_scope_routes.py` replays scripted conversations against the REAL
normalizer and scope classifier, so its expectations are coupled to the prompt and
decision surface. If you change any of:

- `prompts/system.py` (`NORMALIZE_SYSTEM_PROMPT`)
- `agents/normalize.py`
- `prompts/builders.py` (`scope_request` and its four questions)
- `decision.py` (`decide_scope`, `LOG_REQUEST_THRESHOLD`, `QUERYABLE_SCOPE_THRESHOLD`,
  `MAX_CLARIFY_ROUNDS`, `PROCEED_THRESHOLD`)

then revisit `CONVERSATIONS` in that file: update only the cases whose expectations the
change actually affects, run `bash ./routes.sh`, and treat any flipped row as a product
signal before touching it - re-run once to rule out stochasticity, then decide whether
the *input* is wrong (fix the input) or the *product* is wrong (leave it red). Never
re-baseline an expectation to match observed output, and never rewrite the whole list.

## Known failures

Not every entry below is pre-existing — see each bullet's provenance. Offline-only run (no DB, no model calls): `uv run pytest -q --ignore=tests/test_db_queries.py` currently reports `3 failed, 165 passed, 5 skipped` (the 5 skips are the gated live-route conversations).

- All three failures are in `tests/test_rendering.py` (`test_marker_swapped_for_table`, `test_unknown_marker_renders_nothing`, `test_trailing_punctuation_moves_before_marker`) — inline-marker/prose splitting in `ui/rendering.py`'s `canonicalize`. That test file and that module are byte-identical to HEAD and do not depend on the pipeline, so they are pre-existing, not regressions.
- `POST /chat/stream` used to tear on every route that reaches `find_evidence` (now fixed; see the end of this bullet): the root `messages` branch at `src/backend/api.py:192` did `item[-1].text`, but langgraph v3 yields a `ChatModelStream` per root-scope LLM call, so it raised `TypeError: 'ChatModelStream' object is not subscriptable` — status 200 (headers already flushed), partial NDJSON including the `tool_progress` ticks, then the stream dies before the final `messages` event so the answer never arrives, and background `/chats/{id}/investigate` runs end `status=error` with that TypeError as `run.error`. The line is present at HEAD but only became reachable once the evidence report moved off-agent to a direct root-scope `deepseek.with_structured_output(...)` call (`agents/evidence_report.py`, invoked from `find_evidence_node`); at HEAD it ran inside a `create_agent` subgraph, so no root-scope `messages` event existed, and routes whose LLM work stays in subgraphs (for example `clarify`) complete normally — route-dependent, not a dependency regression (`langgraph` is `1.2.12` at HEAD and in the working tree). The fix has landed: the root `messages` branch (`api.py:191-201`) no longer indexes blindly — it logs `item[-1].text` only for a non-empty list/tuple (via `getattr(..., "text", None)`) and otherwise logs the item type, so a `ChatModelStream` cannot raise. That branch is debug-only (`logger.debug`, no `yield`), and the answer is still sourced from the `values` channel into `last_answer` and emitted at `api.py:234`. For pipeline-level end-to-end checks, drive `chatbot.stream_events(...)` and read the `values`/`subgraphs`/`custom` channels instead of the `messages` branch.
- The DB tools' AI endpoint is called by SereneDB inside its own container, so the host being unable to reach `host.docker.internal:11434` does not block DB queries or `tests/test_db_queries.py`.

## Pipeline & API notes

- Graph: `normalize -> scope -> (clarify | general | direct_answer | find_evidence) -> answer -> confidence`, with `confidence` looping back to `find_evidence`/`answer`. There is no separate `direct_answer` node — that scope route maps to `answer`. Scope and confidence are TypeSafe classifiers (`decision.py`); thresholds live there (`LOG_REQUEST_THRESHOLD`, `QUERYABLE_SCOPE_THRESHOLD`, `PROCEED_THRESHOLD`, `FOLLOW_UP_THRESHOLD`, `MAX_CLARIFY_ROUNDS`) and per-effort budgets in `effort.py`. Model instances are in `models.py` (Groq `gpt-oss-20b`/`120b`, DeepSeek, Gemini).
- `scope_request` sends the RAW `conversation` as `transcript` — never `state["normalized"]`, the normalizer's rewrite, which drops the wording that names queryable targets — plus `latest_message`, `latest_answer`, `mission`, `database_schema`, and `tool_catalog`, and asks four questions: `is_log_request`, `has_queryable_scope`, `proceed_without_clarification`, `route`. `decide_scope` routes `general` when `is_log_request < LOG_REQUEST_THRESHOLD`; otherwise it clarifies when `has_queryable_scope < QUERYABLE_SCOPE_THRESHOLD` (no queryable target named) and the user has not authorized proceeding, and otherwise takes the route.
- `decision.py` builds `TypeSafeClassifier()` at import with no model argument, so the SDK default selects the release; the gate thresholds were live-tuned against whatever it resolved to at the time, so a model release can move them — so importing `backend.pipeline.decision` (or `backend.api`) needs `TYPESAFE_API_KEY`. `decision.py` uses `effort.py`'s `max_iterations` (the confidence revision-loop cap); the old per-effort confidence-threshold field no longer exists.
- HTTP surface is broader than `/chat/stream`: background investigation jobs (`POST /chats/{id}/investigate`, poll `GET /chats/{id}/run/{run_id}`, plus `active-run`/`cancel`) backed by `backend/jobs.py`, plus `/chat/reframe`, auth, chat CRUD, and `/healthz`/`/readyz`/`/metrics`. `/chat/test` returns canned NDJSON for tests.
- `/chat/stream` emits NDJSON events (`messages`, `tool_results`, `tool_progress`, `subgraph_name`, `subgraph_message`). Answers use `[[RESULT:Rn]]` markers serialized by `pipeline/answers.py` and rendered by `ui/rendering.py`; the serializer and renderer must stay in sync with the `id` fields on tool results.
- DB query functions return `(sql.Composable, params)` and are wrapped by `query_runner`; each query sets `sdb_ai_allow_insecure_endpoint = true`.
- `find_evidence` is a **single deep agent** (`create_deep_agent`, deepagents) that calls the six DB tools itself — there is no subagent and no `task` tool (the deepseek harness profile in `agents/find_evidence.py` disables the auto-added general-purpose subagent). It keeps `read_file` (how the skill text is disclosed) and `write_todos`; `_EXCLUDED_TOOLS` strips `write_file`, `edit_file`, `delete`, `execute`, `glob`, `grep`, `ls` from the model request.
- `skills/` at the repo root is **mandatory** — `agents/find_evidence.py` raises at import if it is missing — and it is the `FilesystemBackend` root, so `read_file` cannot reach anything else in the repo. This is NOT `.agents/skills/` (an unrelated local skills dir); the pipeline loads the repo-root `skills/`.
- The evidence report is built **off-agent** by `agents/evidence_report.py::generate_evidence_report`, which makes a plain `deepseek.invoke(messages)` call (**no** `response_format`) and validates `message.text` locally with `EvidenceReport.model_validate_json`. The structured-output guarantee is deliberately omitted: in any form (`with_structured_output(..., method="json_mode")`, `.bind(response_format=...)`, or an `invoke(..., response_format=...)` kwarg) `response_format` collapses the streamed reply to exactly one delta, so the model's reasoning could not render token-by-token in the live progress panel. `generate_normalized` (`agents/normalize.py`) and `generate_answer` (`agents/answer.py`) follow the same pattern. Because no schema is transmitted, the JSON contract must ride in the prompt text (`JSON_OUTPUT_SPEC`) — json mode transmitted no schema either, and pydantic `Field` descriptions never reach the model — and every closed vocabulary must be enumerated there (e.g. `tool_used[].status` is exactly `success` or `failure`; without that the model writes `"failed"` and validation fails). (`AIMessage.text` is the text blocks only, so the reasoning blocks are excluded from the parsed payload.)
- The report path is **guard-free**: no `try`/`except`, no empty-report fallback, no citation filtering, no re-validation. A provider failure or contract violation propagates out of `find_evidence_node`; `api.py::chat_stream` is `try`/`finally` with no `except`, so it tears the NDJSON stream, while background investigations record `status=error`.
- Tool results: `tools/results.py::collect_tool_results` reads the agent's own messages and assigns `id` (`R{n}`) and `row_count` inline, so those records ARE the display contract `{id, tool_name, arguments, row_count, rows}` consumed by `answers.py` (`valid_ids`), `api.py` and `ui/rendering.py`. `rows` is capped at 50 for display only; the tools themselves return full row sets into the agent's context.
- Budgets: `effort.py` sets ONE all-tools `ToolCallLimitMiddleware(run_limit)` per effort — Low 4, Medium 8, High 10, Extreme 16, Brutal 20 — and to-do writes count against it. `max_iterations` remains the confidence revision-loop cap.
- Tool progress reaches the UI only from depth ≤ 1. The DB tools emit `tool_progress` payloads on the graph's custom stream from inside the tool (`tools/executor.py`), and failures are re-raised as a generic `RuntimeError` so `handle_tool_errors` can convert them into a ToolMessage. The API's projection captures the root `custom` scope plus ONE level of direct-child `custom` only, so an event emitted from a deeper namespace never reaches the UI. Keep tool calls on the main agent for this reason.
