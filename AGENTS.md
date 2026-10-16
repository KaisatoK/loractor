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
- Seed the DB: `bash ./setup.sh` (no arguments) loads both local corpora through the loghub loader (`uv run python -m backend.db.loghub <dataset> --no-embed`): first `openstack` -- one table from `src/backend/data/openstack_normal1.log`, `openstack_normal2.log`, `openstack_abnormal.log` read in that order, `LineId` running on across them (207,820 rows) -- then `bgl` from `src/backend/data/BGL.log` (or `BGL_LOG_PATH`; 4.7M rows, slow). `set -e`: a missing source file stops it with the loader's message. Each load drops and rebuilds its table. Needs SereneDB on `localhost:7890` (`postgres`/`postgres`, db `postgres`); no embedding model is needed with `--no-embed`. There is no `backend.db.setup` module and no `TABLE_NAME` constant any more, and the old per-file `openstack_normal1`/`openstack_normal2`/`openstack_abnormal` tables were dropped. `openstack_2k`/`bgl_2k` load loghub's 2,000-line structured CSV samples into the same tables instead.
- Provision a user: `uv run python -m backend.appdb.manage <username> <password>`.
- `LOG_LEVEL=DEBUG bash ./backend.sh` prints tool calls, SQL, and query params.
- `.env` (gitignored) is loaded on import by `src/backend/__init__.py` and `src/ui/__init__.py`; it holds `GROQ_API_KEY`, `DEEPSEEK_API_KEY`, `GOOGLE_API_KEY`, `TYPESAFE_API_KEY`, ports, and auth flags (`AUTH_REGISTRATION_OPEN`, `AUTH_DEMO_*`). `TYPESAFE_API_KEY` is required at import — as are the three model keys, because `models.py` builds its chat clients at import time.
- No lint/format/typecheck/CI is configured (none of ruff/black/mypy/pyright installed). Do not invent those commands.
- `requirements.txt` is **generated**, not hand-edited — it is `uv export` output (the header names the command). Change dependencies in `pyproject.toml` and re-export.
- Line endings: `.gitattributes` sets `* text=auto` and `*.sh eol=lf`. Keep new/edited files LF; untracked files are not normalized by git, so a stray CRLF write persists.
- When scripting against the app, set `CHECKPOINT_BACKEND=memory` (and point `APP_DB_PATH` outside the repo if relevant) so you do not create or mutate `.data/`.

## Testing

- `uv run pytest` (testpaths = `tests`; nearly all offline).
- Single test: `uv run pytest tests/test_decision_nodes.py::test_scope_routes_route_when_queryable_scope_present`.
- `tests/conftest.py` sets dummy `GROQ_API_KEY` / `DEEPSEEK_API_KEY` / `GOOGLE_API_KEY` + `CHECKPOINT_BACKEND=memory` before backend import. `test_decision_nodes.py` additionally sets `TYPESAFE_API_KEY` before importing `decision.py`, because that module builds a live `TypeSafeClassifier` at import time.
- Live corpus tests are gated and skip by default: `RUN_OPENSTACK_LIVE=1` (`tests/test_openstack_live.py`), `RUN_BGL_LIVE=1` (`tests/test_bgl_live.py`), `RUN_OTEL_LIVE=1` (`tests/test_otel_live.py`). The first two need the corpus loaded (`bash ./setup.sh`) and skip when its table is absent. Their offline twins `test_openstack.py` / `test_bgl.py` cover registration, parsers and SQL shape.
- Live route tests (`tests/test_live_scope_routes.py`): skipped offline; run alone with `bash ./routes.sh` (sets `RUN_LIVE_ROUTES=1`) — real normalize + scope-classifier calls, ~2 per asserted turn. Must not run inside the full suite (conftest installs dummy keys before the backend's import-time clients; the module reloads `.env` with `override=True` only when gated). Gated runs write the HTML report `.data/scope_routes_report.html` (gitignored; override `RUN_LIVE_ROUTES_REPORT`; its path is printed once per run), and the per-turn numbers appear in an assertion failure. Routes are stochastic — a flipped row shows as `✗` with raw numbers; re-run before treating it as a regression.
- `test_answer_rendering.py` is pure/offline and asserts the serializer and UI renderer stay in sync.
- Live confidence tests (`tests/test_live_confidence.py`): skipped offline; run alone with `bash ./confidence.sh` (sets `RUN_LIVE_CONFIDENCE=1`) — real atomic confidence judgments and code-owned revision policy. Gated runs write `.data/confidence_report.html` (override `RUN_LIVE_CONFIDENCE_REPORT`). If confidence prompts or policy thresholds change, revisit the representative cases, re-run once on a flip, and classify the result as an input or product issue; never re-baseline to match observed output.

### Live route cases are coupled to normalize/scope

`tests/test_live_scope_routes.py` replays scripted conversations against the REAL
normalizer and scope classifier, so its expectations are coupled to the prompt and
decision surface. If you change any of:

- `prompts/system.py` (`NORMALIZE_SYSTEM_PROMPT`)
- `agents/normalize.py`
- `prompts/builders.py` (`scope_request` and its four questions, `QUERYABLE_TARGETS`)
- `decision.py` (`decide_scope`, `LOG_REQUEST_THRESHOLD`, `QUERYABLE_SCOPE_THRESHOLD`,
  `NEW_EVIDENCE_THRESHOLD`, `MAX_CLARIFY_ROUNDS`, `PROCEED_THRESHOLD`)

then revisit `CONVERSATIONS` in that file: update only the cases whose expectations the
change actually affects, run `bash ./routes.sh`, and treat any flipped row as a product
signal before touching it - re-run once to rule out stochasticity, then decide whether
the *input* is wrong (fix the input) or the *product* is wrong (leave it red). Never
re-baseline an expectation to match observed output, and never rewrite the whole list.
`QUERYABLE_TARGETS` recently dropped "event id" (no corpus has one), so the live
routes need a re-run.

### Live confidence cases are coupled to confidence policy

`tests/test_live_confidence.py` replays representative evidence and answer states
against the real atomic confidence judgments. If `confidence_request`, any
confidence question, `decide_confidence`, or `EVIDENCE_REVISION_THRESHOLD` /
`ANSWER_OK_THRESHOLD` changes, revisit the cases and run `bash ./confidence.sh`.
A flipped case is a product signal: re-run once, then fix the input or product;
never re-baseline an expectation just to make the run green.

## Known failures

Not every entry below is pre-existing — see each bullet's provenance. Offline-only run (no DB, no model calls): `uv run pytest -q` currently reports `3 failed, 612 passed, 53 skipped` (the skips are the gated live corpus, route and confidence cases).

- All three failures are in `tests/test_rendering.py` (`test_marker_swapped_for_table`, `test_unknown_marker_renders_nothing`, `test_trailing_punctuation_moves_before_marker`) — inline-marker/prose splitting in `ui/rendering.py`'s `canonicalize`. That test file and that module are byte-identical to HEAD and do not depend on the pipeline, so they are pre-existing, not regressions.
- `POST /chat/stream` used to tear on every route that reaches `find_evidence` (now fixed; see the end of this bullet): the root `messages` branch at `src/backend/api.py:192` did `item[-1].text`, but langgraph v3 yields a `ChatModelStream` per root-scope LLM call, so it raised `TypeError: 'ChatModelStream' object is not subscriptable` — status 200 (headers already flushed), partial NDJSON including the `tool_progress` ticks, then the stream dies before the final `messages` event so the answer never arrives, and background `/chats/{id}/investigate` runs end `status=error` with that TypeError as `run.error`. The line is present at HEAD but only became reachable once the evidence report moved off-agent to a direct root-scope `deepseek.with_structured_output(...)` call (`agents/evidence_report.py`, invoked from `find_evidence_node`); at HEAD it ran inside a `create_agent` subgraph, so no root-scope `messages` event existed, and routes whose LLM work stays in subgraphs (for example `clarify`) complete normally — route-dependent, not a dependency regression (`langgraph` is `1.2.12` at HEAD and in the working tree). The fix has landed: the root `messages` branch (`api.py:191-201`) no longer indexes blindly — it logs `item[-1].text` only for a non-empty list/tuple (via `getattr(..., "text", None)`) and otherwise logs the item type, so a `ChatModelStream` cannot raise. That branch is debug-only (`logger.debug`, no `yield`), and the answer is still sourced from the `values` channel into `last_answer` and emitted at `api.py:234`. For pipeline-level end-to-end checks, drive `chatbot.stream_events(...)` and read the `values`/`subgraphs`/`custom` channels instead of the `messages` branch.
- The DB tools' AI endpoint is called by SereneDB inside its own container, so the host being unable to reach `host.docker.internal:11434` does not block DB queries.

## Pipeline & API notes

- Graph: `normalize -> scope -> (clarify | general | direct_answer | find_evidence) -> answer -> confidence`, with `confidence` looping back to `find_evidence`/`answer`. There is no separate `direct_answer` node — that scope route maps to `answer`. Scope and confidence use atomic TypeSafe Nouls (`decision.py`); code composes their probabilities into policy routes. Scope thresholds are `LOG_REQUEST_THRESHOLD`, `QUERYABLE_SCOPE_THRESHOLD`, `PROCEED_THRESHOLD`, `NEW_EVIDENCE_THRESHOLD`, `FOLLOW_UP_THRESHOLD`, and `MAX_CLARIFY_ROUNDS`; confidence thresholds are `EVIDENCE_REVISION_THRESHOLD` and `ANSWER_OK_THRESHOLD`; per-effort revision budgets live in `effort.py`. Model instances are in `models.py` (Groq `gpt-oss-20b`/`120b`, DeepSeek, Gemini).
- `scope_request` sends the RAW structured `messages` transcript — never `state["normalized"]`, the normalizer's rewrite — plus `latest_message`, `latest_answer`, `mission`, `queryable_targets`, and `tool_catalog`, and asks four Nouls: `is_log_request`, `has_queryable_scope`, `proceed_without_clarification`, `needs_new_evidence`. `decide_scope` routes `general` when `is_log_request < LOG_REQUEST_THRESHOLD`; otherwise it clarifies when `has_queryable_scope < QUERYABLE_SCOPE_THRESHOLD` and the user has not authorized proceeding, and otherwise uses `needs_new_evidence` in code to select `direct_answer` or `find_evidence`.
- `confidence_request` asks independent Nouls about `evidence_relevant`, `evidence_gap_remains`, `gap_closable_by_untried_tool`, `answer_grounded`, `answer_fits_role`, `answer_complete`, and `worth_follow_up`. `decide_confidence` owns the revision route and reason in code, preserving the raw signals in state; Noul probabilities are not separate confidence values.
- `decision.py` builds `TypeSafeClassifier()` at import with no model argument, so the SDK default selects the release; the gate thresholds were live-tuned against whatever it resolved to at the time, so a model release can move them — so importing `backend.pipeline.decision` (or `backend.api`) needs `TYPESAFE_API_KEY`. `decision.py` uses `effort.py`'s `max_iterations` (the confidence revision-loop cap); the old per-effort confidence-threshold field no longer exists.
- HTTP surface is broader than `/chat/stream`: background investigation jobs (`POST /chats/{id}/investigate`, poll `GET /chats/{id}/run/{run_id}`, plus `active-run`/`cancel`) backed by `backend/jobs.py`, plus `/chat/reframe`, auth, chat CRUD, and `/healthz`/`/readyz`/`/metrics`. `/chat/test` returns canned NDJSON for tests.
- `/chat/stream` emits NDJSON events (`messages`, `tool_results`, `tool_progress`, `subgraph_name`, `subgraph_message`). Answers use `[[RESULT:Rn]]` markers serialized by `pipeline/answers.py` and rendered by `ui/rendering.py`; the serializer and renderer must stay in sync with the `id` fields on tool results.
- DB query functions return `(sql.Composable, params)` and are wrapped by `query_runner`; each query sets `sdb_ai_allow_insecure_endpoint = true`. Every builder takes a `dataset` id resolved through `db/datasets.py` and composes SQL from canonical fields (`ts`, `severity`, `service`, `message`, `entity_id`, `template`, `record_id`) -- tool arguments `category`/`service` map to the `Level`/`Component` columns on both local corpora. Every pooled and probe connection runs `SET TimeZone = 'UTC'` (`connection.set_session_time_zone`), so naive window bounds mean UTC everywhere.
- Loaders live in `db/loghub.py`: one `LoghubSpec` per corpus (`bgl`, `bgl_2k`, `openstack`, `openstack_2k`) with its own `line_parser` (`parse_bgl_line` locates fields by the `Level`/`Type` vocabulary; `parse_openstack_line` is a positional regex with a bracketed `ADDR`), `raw_names` (several files share one table with a running `LineId`), and per-spec `header_renames` (only `bgl_2k` renames its CSV `Timestamp`/`Time` headers). `EventTemplate` is derived at load; there is no event id, pid text mirror or per-row embedding.
- Auto-complete selectors are `@` service, `^` template, `$` message text; `!` is only the case-sensitivity modifier after a selector. The only public lookup is `db.auto_complete(query)`, used by `/auto-complete` (no `dataset` parameter; always in-process -- the MCP service has no auto-complete tool): it runs the private per-corpus `_auto_complete_dataset` over the local `.log` corpora (`openstack`, `bgl`), merges the results and tags `$` suggestions with `dataset`; the chat box inserts `Line ID: n (dataset)`.
- `find_evidence` is a **single deep agent** (`create_deep_agent`, deepagents) that calls the nine DB tools itself (seven bounded query tools plus `sweep_chunks_tool`/`investigate_chunk_tool`) — there is no subagent and no `task` tool (the deepseek harness profile in `agents/find_evidence.py` disables the auto-added general-purpose subagent). It keeps `read_file` (how the skill text is disclosed) and `write_todos`; `_EXCLUDED_TOOLS` strips `write_file`, `edit_file`, `delete`, `execute`, `glob`, `grep`, `ls` from the model request.
- The evidence agent's **state channels depend on the transport**, so tests that
  assert the exact set must branch on `get_db_tools()[1]` (`mcp_enabled`), not on
  `TOOLS_TRANSPORT`: that env var says `mcp` even when the service is unreachable
  and the registry has fallen back to local tools. Local transport passes a
  disk-backed `FilesystemBackend` rooted at `skills/`, giving
  `{messages, structured_response, todos}`. Under MCP the skill text comes from
  the `list_skills`/`read_skill` tools, no backend is passed, and deepagents
  installs its default **state-backed** store, which adds an (unused) `files`
  channel. The transport-independent invariant is that no channel carries the
  report -- it is built off-agent. See the two tests at the end of
  `tests/test_find_evidence.py`.
- `skills/` at the repo root is **mandatory** — `agents/find_evidence.py` raises at import if it is missing — and it is the `FilesystemBackend` root, so `read_file` cannot reach anything else in the repo. This is NOT `.agents/skills/` (an unrelated local skills dir); the pipeline loads the repo-root `skills/`.
- The evidence report is built **off-agent** by `agents/evidence_report.py::generate_evidence_report`, which makes a plain `deepseek.invoke(messages)` call (**no** `response_format`) and validates `message.text` locally with `EvidenceReport.model_validate_json`. The structured-output guarantee is deliberately omitted: in any form (`with_structured_output(..., method="json_mode")`, `.bind(response_format=...)`, or an `invoke(..., response_format=...)` kwarg) `response_format` collapses the streamed reply to exactly one delta, so the model's reasoning could not render token-by-token in the live progress panel. `generate_normalized` (`agents/normalize.py`) and `generate_answer` (`agents/answer.py`) follow the same pattern. Because no schema is transmitted, the JSON contract must ride in the prompt text (`JSON_OUTPUT_SPEC`) — json mode transmitted no schema either, and pydantic `Field` descriptions never reach the model — and every closed vocabulary must be enumerated there (e.g. `tool_used[].status` is exactly `success` or `failure`; without that the model writes `"failed"` and validation fails). (`AIMessage.text` is the text blocks only, so the reasoning blocks are excluded from the parsed payload.)
- The report path is **guard-free**: no `try`/`except`, no empty-report fallback, no citation filtering, no re-validation. A provider failure or contract violation propagates out of `find_evidence_node`; `api.py::chat_stream` is `try`/`finally` with no `except`, so it tears the NDJSON stream, while background investigations record `status=error`.
- Tool results: `tools/results.py::collect_tool_results` reads the agent's own messages and assigns `id` (`R{n}`) and `row_count` inline, so those records ARE the display contract `{id, tool_name, arguments, row_count, rows}` consumed by `answers.py` (`valid_ids`), `api.py` and `ui/rendering.py`. `rows` is capped at 50 for display only; the tools themselves return full row sets into the agent's context.
- Budgets: `effort.py` sets ONE all-tools `ToolCallLimitMiddleware(run_limit)` per effort — Low 5, Medium 9, High 11, Extreme 17, Brutal 21 — and to-do writes count against it. `max_iterations` remains the confidence revision-loop cap.
- Tool progress reaches the UI only from depth ≤ 1. The DB tools emit `tool_progress` payloads on the graph's custom stream from inside the tool (`tools/executor.py`), and failures are re-raised as a generic `RuntimeError` so `handle_tool_errors` can convert them into a ToolMessage. The API's projection captures the root `custom` scope plus ONE level of direct-child `custom` only, so an event emitted from a deeper namespace never reaches the UI. Keep tool calls on the main agent for this reason.

### MCP tool service

`mcp_server.sh` runs the standalone FastMCP Streamable HTTP service on
`127.0.0.1:8010/mcp`. The pipeline defaults to local tools; set
`TOOLS_TRANSPORT=mcp` to use the server for the nine DB tools and skills.
`/auto-complete` is not on the server: it always runs in-process. The
synchronous pipeline uses a dedicated asyncio loop thread for
MCP calls and falls back to local DB tools when the service is unavailable.
Skills are exposed by the server as `skill://` resources and `list_skills` /
`read_skill` tools; local transport retains the sandboxed filesystem backend.
`fastmcp` is pinned in `pyproject.toml`, and `requirements.txt` must be
regenerated with `uv export`, not edited manually. Loopback is unauthenticated;
set `MCP_AUTH_TOKEN` for non-loopback binding.
