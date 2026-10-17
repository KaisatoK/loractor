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
  openstack · bgl (4.7M) · otel_s3 (1e9, in place) · otel_slice (in-place index)
```

The `scope` stage uses four independent TypeSafe (Jev) Noul judgments: whether
`latest_message` is a log request, whether the raw message transcript names a
queryable target, whether the user authorized proceeding without clarification,
and whether new log evidence is needed. Code owns the policy: non-log requests go
to a general reply; vague log requests are clarified unless authorized or out of
clarification budget; otherwise `needs_new_evidence` selects a direct answer or
investigation. The **evidence stage** (`find_evidence`) is a deep agent: it plans the
investigation and calls the database tools itself, then submits a free-text summary
of its findings. The report of `summary` plus `evidence` (and optional
`hypotheses`) is built off-agent from that summary and the captured results, which
are renumbered `R1..Rn` for the answer tables. A bundled `log-investigation` skill
decides when hypotheses apply.

## Log corpora and how they are queried

The chatbot investigates four corpora through one registry, the largest of which
is **a billion rows of OpenTelemetry logs held as Parquet in public S3**. That
corpus is never downloaded: it is queried in place, and the database it is
queried from stays inside a declared budget of **3 GiB of disk and a 512 MiB
memory limit**.

### Why this needs an algorithm at all

Reading the whole corpus is not an option — reading just the message text of one
of its fifty files takes about twelve days. What makes the problem tractable is
that **reads do not all cost the same**, by three orders of magnitude:

| Operation | Measured cost | How much it can cover |
| --- | --- | --- |
| Read Parquet footers (row counts, min/max per column) | negligible | the whole corpus |
| Aggregate on timestamp + severity | ~1s per window | the whole corpus |
| Build an inverted index **in place** over the files | **470s, fixed** | ~15 row groups per build |
| Read message text with an ordinary `SELECT` | **660s per row group** | one row group at a time |

Every design decision below follows from that table. Metadata is free, so it is
read exhaustively; message text is expensive, so it is read last, narrowly, and
only where cheaper evidence already points.

Notably, the in-place index's build cost is **flat, not per row**: 72,678 rows
and 1,401,135 rows both index in ~465s. Indexing a wider window is therefore
nearly free, which is why the searchable tier covers a window rather than a
sample.

### The dataset registry

Each corpus is a `Dataset` in `src/backend/db/datasets.py`, declaring its columns
under canonical names (`ts`, `severity`, `service`, `message`, `entity_id`,
`template`, `record_id`) plus the capabilities it can honestly honour. Query
builders in `src/backend/db/` compose SQL from those declarations, never from a
table name supplied by the model.

| Dataset | Kind | Rows | Capabilities | Role |
| --- | --- | --- | --- | --- |
| `openstack` | local table | 2,000 | BM25, per-row vectors, exact counts, templates, auto-complete, record id | default; demos and regression tests |
| `bgl` | local table | 4,747,963 | BM25, template vectors, exact counts, templates, auto-complete, record id | Blue Gene/L RAS log; spans seven months, so it answers long-range questions |
| `otel_s3` | remote Parquet | 1,000,000,000 | approximate aggregates, chunk sweep, templates | the corpus itself, read in place |
| `otel_slice` | **in-place index** | — | BM25, exact counts | text search over the remote corpus |

Because builders read the registry rather than hard-coded SQL, adding BGL as a
fourth corpus required **no new SQL in any builder**.

Capabilities are declared, not assumed, and a missing one is refused at
registration rather than failing at query time. `otel_slice` deliberately
declares neither `templates` nor `auto_complete` nor `record_id`: all three
needed columns that only existed while rows were being copied locally, and that
copy step no longer exists.

### Three tiers

**Tier A — local, indexed, exact.** `openstack` and `bgl`. Millisecond searches,
exact counts, embeddings for semantic reach. BGL's embeddings are
**template-level**: 20,352 distinct templates stand in for 4.7M rows, which is
what makes semantic search affordable at that size (embedding every row would
take days; embedding the templates took 36 minutes).

**Tier B — remote, metered, approximate.** `otel_s3`, read via `read_parquet`.
Every query must carry a time window of at most 300 seconds. Before running,
Parquet footers are read to decide which row groups the window can possibly
touch, and that plan becomes the query's own cost estimate. A 10-second window
prunes to 10 of 1,624 row groups — over 99% skipped. Aggregates from this tier
are **labelled approximate**, and its disk allowance is **zero bytes**, so
nothing can be built here by construction.

**Tier C — searchable, in place.** `otel_slice` is an inverted index built
directly over the remote Parquet. Rows stay in S3; only the search postings are
stored locally. A match is found in the index, then its row is re-read from the
file — so a search costs ~300ms–2s rather than ~20ms, and **nothing is
downloaded**. This replaced an earlier approach that copied rows into a local
table; indexing in place builds in 465s instead of 1,324s, stores 45–60 bytes
per row instead of 155, and copies nothing.

### The sweep: covering a billion rows on a laptop

When a question genuinely needs evidence from the whole corpus, the sweep runs
four passes, each roughly 100× narrower than the last. It is implemented in
`src/backend/pipeline/sweep.py` with scoring in `triage.py`.

**1. Map** — read footers only. The corpus is tiled into **1,624 chunks** of one
row group each (~614k rows, ~1.1s of log time). Chunks tile on *successor*
boundaries because adjacent row groups overlap in time; deriving each chunk's end
from its own maximum timestamp produced 445 overlapping pairs and risked dropping
rows at boundaries. Verified: 1,624 chunks, exactly 1,000,000,000 rows, no gaps
and no overlaps.

**2. Profile** — one grouped aggregate per batch of 120 chunks, over timestamp
and severity columns only, reading **no message text**. All 1,624 chunks cost
~14 queries and ~28 seconds. A chunk is discarded only on the *absence* of
signal — no problem-severity rows at all, or none of the services the question
named — because that is the strongest claim available to a pass that has read no
text.

**3. Sample** — the top-ranked survivors (at most 12) get 8 windows of 250 rows
each: 2,000 rows, and the first message text read. Each chunk is scored
`0.5 × term hits + 0.3 × error density + 0.2 × service overlap`.

**4. Promote** — the best chunks (2 at Extreme effort, 4 at Brutal) are indexed,
summarised into a **digest** — counts, top 10 templates, 5 verbatim exemplars
with file and timestamp — and then **dropped**. Teardown is unconditional and
verified to return disk exactly to baseline.

The digest is what allows coverage beyond what the disk can hold: a promoted
chunk is ~14 MiB and 1,624 of them will not fit, but a digest is a few kilobytes
and outlives the chunk it came from.

### How the agent uses it

The evidence agent has nine tools. Seven are ordinary bounded queries; two are
the sweep (`sweep_chunks_tool`, `investigate_chunk_tool`).

The sweep tools are **withheld entirely below Extreme effort** rather than
offered and refused — a visible tool gets tried eventually, and a refusal still
consumes one of the 4–20 calls the effort level permits. `max_promotions` is the
existing per-effort dial, so it governs this too:

| Effort | Tool calls | Rows scanned | Promotions |
| --- | --- | --- | --- |
| Low | 4 | 25M | 0 |
| Medium | 8 | 100M | 0 |
| High | 10 | 250M | 0 |
| Extreme | 16 | 1B | 2 |
| Brutal | 20 | 5B | 4 |

The whole sweep runs **inside a single tool call**, because 1,624 chunks cannot
be 1,624 calls when the ceiling is 20. The agent decides *whether* to sweep and,
afterwards, which single chunk deserves a full text read. Calling
`sweep_chunks_tool` with `confirm_cost=false` returns a cost estimate instead of
running, so the user can agree to the spend first — four promotions is about
three quarters of an hour.

**Refusals are answers, not errors.** Asking for a 10-minute window on the
remote tier returns *"would scan ~340M rows; narrow to 300s or add a severity
filter"* in under five seconds without touching the corpus. Every refusal
carries a concrete suggestion, and the agent's correct next move is to follow it
and retry. The UI renders refusals distinctly from failures.

### Observability

Because the sweep's entire cost sits inside one tool call, it reports progress
from the inside, as a stream of stages (`map`, `profile`, `sample`, `decide`,
`promote`, `done`). A real run:

```
[map     ] Mapped 1,624 chunks covering 1,000,000,000 rows from file metadata only.
[profile ] Profiled chunks 0-0..119-119 (120/1,624); kept 120 of 120 for sampling.
[sample  ] Sampled part_000:14-14: 2000 rows read, score 0.25 (ambiguous)
           -- 467/2000 matched ['failed','charge','card']
[decide  ] Promoting part_000:14-14 (score 0.25). Estimated 660s to read its text.
[promote ] Read part_000:14-14: 92,398 problem rows from 622,592 scanned.
[done    ] Swept 120 of 1,624 chunks in 558s: 2 sampled, 1 read in full.
```

Every event carries the score's three component signals and a
`reads_message_text` flag — which is what explains why profiling covered the
whole corpus for free while promoting one chunk cost nine minutes. The trace is
streamed to the UI as `tool_progress`, logged, and kept on the result for
auditing; `ui/rendering.py` shows it in a collapsed panel.

### Three kinds of honesty, enforced in code

**Approximation is labelled.** Remote aggregates carry `approximate: true`,
because they are computed over pruned row groups rather than the full corpus.

**Sampling reports its own vagueness.** A sample is a *cluster* sample —
consecutive rows from a few windows — so 2,000 rows carry about 8 independent
observations, not 2,000. Uncertainty is computed *between* windows, so two
samples that both pool to 60% are reported differently: **60–60%** when the
chunk is uniform, **26–99%** when the windows disagree. The answer must quote
the range rather than the midpoint. Which chunks get sampled at all is
unavoidably arbitrary, much like choosing a quicksort pivot with no prior
information, and the output says so instead of implying the sample was
representative.

**Coverage is stated.** *"Of 1,624 chunks, 120 were profiled, 2 sampled, and 1
searched in full. Evidence may exist in chunks that were profiled but not
searched."* The three depths are not interchangeable, and the output contract
requires naming them: a profiled chunk was **counted, not read**.

Every answer also carries its cost, computed off-model from the tools' own
metering:

```
Query cost: 2 queries in 2.14s, slowest R1 at 2.11s (99% of the total),
read 1 of 1 files, ~6,327,325 rows scanned, ~222.4 MiB, ~2,959,460 rows/s,
1 of 2 results approximate.
```

### Capacity

Disk is budgeted per dataset and checked before anything is built:

| Allowance | Size |
| --- | --- |
| `bgl` | 1.2 GiB |
| `otel_chunk` (one promoted chunk, transient) | 768 MiB |
| `otel_slice` (the in-place index) | 256 MiB |
| `openstack` | 64 MiB |
| `otel_s3` | **0 B** — nothing may be built from the remote corpus |

Current usage is **380.5 MiB of the 3 GiB budget**, holding the full 4.7M-row BGL
corpus (281 MiB) with all 20,352 of its template embeddings (64 MB).

The memory limit matters more than it looks: it was originally left at 5.7 GiB on
a machine with ~1 GB free, and the result was three crashes caused by the kernel
OOM killer rather than by the database's own accounting. It is now 512 MiB.

### What this does not do

A sweep at Extreme effort covers roughly 7% of one of fifty files in ten minutes.
It is **not exhaustive and never claims to be**. It is a way to spend a bounded
budget where the evidence most plausibly is, and then to say precisely where it
looked and how deeply — so that a negative result is reported as "not found in
what was searched" rather than "not present".

## Prerequisites

- Python 3.14+
- [uv](https://docs.astral.sh/uv/)
- A SereneDB instance on `localhost:7890` (default credentials
  `postgres`/`postgres`, database `postgres`)
- An Ollama server reachable at `host.docker.internal:11434` serving
  `nomic-embed-text:v1.5` for the embedding index
- Nothing extra for the remote OTel corpus: it is read from public S3 in place.
  The BGL corpus is optional and loaded from a local loghub file (`BGL_LOG_PATH`,
  743 MB); the chatbot runs without it, minus the long-range questions.

## Environment

Create `.env` in the repo root with the API keys for the providers in use:

```
GROQ_API_KEY=...
DEEPSEEK_API_KEY=...
GOOGLE_API_KEY=...
TYPESAFE_API_KEY=...

# Optional ports (defaults: 8000 backend, 8501 UI)
BACKEND_PORT=8000
FRONTEND_PORT=8501
```

Keys and ports are loaded automatically on import via `python-dotenv`; all four
provider keys are read at import, so they must be present for the backend to
start. The UI reads `BACKEND_PORT` to reach the backend, so both scripts stay in
sync.

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

## Type-ahead auto-complete

The chat box suggests indexed log-field values as you type a **selector**. The
*current word* (the run of non-space characters the caret sits in) is looked up
through `GET /auto-complete`; the dropdown opens only while the box is focused,
and picking a suggestion replaces the draft words it spans (a multi-word
`event_template` such as `Removable base files: <*>` replaces every word it lines
up with, so nothing is duplicated).

A leading **selector** scopes the lookup to one column. One character is assigned
per column, chosen so that it occurs in *no* indexed value and can therefore
never collide with real text:

| Selector | Column |
| --- | --- |
| `@` | `service` |
| `#` | `event_id` |
| `^` | `event_template` |
| `!` | `pid` |
| `$` | `content` |

So `@nova` suggests services, `#E25` suggests event ids, `^Removable` suggests
event templates, `!257` suggests pids, and `$timeout` suggests log lines whose
content contains `timeout`. (`pid` is an integer column, so it is suggested
through its indexed text mirror `pid_text`; `content` is word-indexed, so it is
matched as whole lines rather than single words.)

Each dropdown entry shows its **count** as `Count: n`. A `$` content entry instead
shows `Line ID: n`, and picking it inserts that `Line ID: n` reference into the
box rather than pasting the whole message. A bare selector lists that column's most
popular values. A selector is **required**: a word without one is not looked up and
suggests nothing. The selector is stripped before matching and is shown as a prefix
on each dropdown entry (a display-only reminder of the column in scope); it is
consumed when a suggestion is picked, so the bare value is inserted.

Matching is always case-insensitive, and by default ranking is too — a value is
never preferred just for matching the case you typed. Typing `!` immediately after
the selector (`@!nova`) switches to a case-sensitive ranking, putting values whose
case matches what you typed first.

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
chat's history on demand. `bash ./backend.sh` provisions the demo account on
start; `bash ./frontend.sh` alone does not, so the UI will not boot until the
account exists.

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
bash ./backend.sh     # uvicorn backend.api:app --port $BACKEND_PORT --reload
bash ./frontend.sh    # streamlit run src/ui/streamlit_app.py --server.port $FRONTEND_PORT
```

Both commands run from the repository root. Set `BACKEND_PORT`/`FRONTEND_PORT`
in `.env` (or the environment) to run on non-default ports; `8000`/`8501` are
used when unset. The scripts are not executable — invoke them with `bash`.

## Testing

```bash
uv run pytest
```

The suite in `tests/test_db_queries.py` runs the database query layer against a
**live SereneDB**. It requires the DB to be running on `localhost:7890` and
seeded (see Setup); if SereneDB is unreachable or empty the tests **fail
loudly** rather than skip.

Everything else runs without external services (the gated live route tests below are skipped unless enabled):

| File | Covers |
| --- | --- |
| `test_auth.py` | scrypt hashing, users, sessions, expiry, bearer dependency |
| `test_chats.py` | chat/message CRUD, ordering, upsert, cascade delete, isolation |
| `test_api.py` | auth gating, chat endpoints, NDJSON streaming, admission limits, request ids, metrics |
| `test_jobs.py` | background run registry and investigation endpoints (buffer, re-attach, cancel, persistence) |
| `test_middleware.py` | error classification and retry/backoff |
| `test_checkpointer.py` | checkpointer backend selection |
| `test_config.py` | env-driven config and the pooled connection layer |
| `test_observability.py` | metrics registry and request-correlation middleware |
| `test_chat_client.py` / `test_rendering.py` / `test_manage.py` / `test_demo.py` | UI client, marker rendering, provisioning CLI, demo auto-login |
| `test_decision_nodes.py` / `test_confidence_prompts.py` / `test_live_confidence.py` / `test_find_evidence.py` | scope & confidence decisions, atomic classifier payloads, gated live confidence checks, evidence-node wiring |
| `test_stream_projection.py` / `test_tool_progress.py` / `test_answer_rendering.py` | streaming projection, tool-progress events, serializer↔renderer sync |
| `test_query_layer.py` / `test_budget.py` / `test_search_terms.py` | dataset registry, bounded/metered queries, disk budget, tokenizer |
| `test_templates.py` / `test_template_build.py` | template extraction, and that the embedding loop provably terminates |
| `test_bgl.py` / `test_bgl_live.py` | loghub parsing; gated live checks against the full 4.7M-row corpus |
| `test_sampling_and_external.py` / `test_promotion.py` | cluster-sample uncertainty, in-place index plans, promote/digest/teardown |
| `test_sweep*.py` (4 files) | sweep orchestration, effort gating, observability trace, scoring |
| `test_otel_live.py` | gated live checks against the remote corpus and its in-place index |

> The offline suite reports `3 failed, 578 passed, 42 skipped`. The three
> failures are known and pre-existing, all in `tests/test_rendering.py`
> (inline-marker rendering edge cases); the skips are the gated live cases
> below. The suite passes identically under `TOOLS_TRANSPORT=local` and
> `TOOLS_TRANSPORT=mcp`.

### Live corpus tests

Two suites run against the real corpora and are skipped unless gated, because
each costs real queries against a live database (and, for OTel, public S3):

```bash
RUN_BGL_LIVE=1  uv run pytest tests/test_bgl_live.py     # full 4.7M-row BGL
RUN_OTEL_LIVE=1 uv run pytest tests/test_otel_live.py    # remote corpus + index
```

`RUN_BGL_LIVE` needs BGL loaded (`uv run python -m backend.db.loghub bgl`, which
reads `BGL_LOG_PATH`). `RUN_OTEL_LIVE` needs only network access; the tests that
require the in-place index skip themselves with a message when it has not been
built, rather than failing.

### Live route tests

`bash ./routes.sh` replays each scripted conversation in
`tests/test_live_scope_routes.py` against the real normalizer and scope
classifier and asserts the route each turn takes (`clarify`, `general`,
`direct_answer`, `find_evidence`). It is gated by `RUN_LIVE_ROUTES=1` (the script
sets it) and must be run alone — never inside the full suite, because
`tests/conftest.py` installs dummy provider keys before the backend's
import-time clients are built. Each gated run writes
`.data/scope_routes_report.html` (gitignored; override with
### Live confidence checks

`bash ./confidence.sh` replays representative evidence and answer states through
the real confidence classifier and checks the code-owned revision policy. It is
gated by `RUN_LIVE_CONFIDENCE=1`, must run alone, and writes
`.data/confidence_report.html` (override with `RUN_LIVE_CONFIDENCE_REPORT`). The
report includes each atomic Noul signal and the selected policy route. Thresholds
are starting values validated against these cases; the cases are stochastic and
must not be re-baselined to match a flipped result.

`RUN_LIVE_ROUTES_REPORT`) and prints its path once; the per-turn numbers appear in
the assertion failure. Offline, only a case-data sanity check runs. Costs two
live API calls per asserted turn, and the classifier is stochastic — re-run
before treating a flipped route as a regression.

To see tool calls and their parameters (and, at `DEBUG`, the SQL and query
params) in the backend terminal:

```bash
LOG_LEVEL=DEBUG bash ./backend.sh
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

### `GET /auto-complete`

Type-ahead suggestions from the indexed log-field values. `query` is required and
must start with a selector scoping it to one column (`@` service, `#` event_id,
`^` event_template, `!` pid, `$` content); a bare selector lists that column's
most popular values, while a query with no leading selector returns no
suggestions.
Returns `{"suggestions": [{"suggestion", "count"}, ...]}`; a `$` content
suggestion carries `line_id` in place of `count`.

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
backend.sh    uvicorn backend.api:app (demo account provisioned on start)
frontend.sh   streamlit run src/ui/streamlit_app.py
routes.sh     gated live route tests (RUN_LIVE_ROUTES=1) + HTML report
confidence.sh gated live confidence-policy tests (RUN_LIVE_CONFIDENCE=1) + HTML report
setup.sh      legacy DB seeding script (prefer: uv run python -m backend.db.setup)
src/
├── backend/
│   ├── api.py            FastAPI endpoints, streaming, and admission control
│   ├── jobs.py           in-process background investigation registry
│   ├── observability.py  request-correlation middleware and metrics registry
│   ├── appdb/            users, sessions, chats, messages (SQLite)
│   ├── db/               pooled SereneDB connection, config, and log queries
│   │   ├── datasets.py     the corpus registry: columns + declared capabilities
│   │   ├── budget.py       disk/memory budget, per-dataset allowances, findings
│   │   ├── corpus.py       measured corpus arithmetic and shared text dictionaries
│   │   ├── pruning.py      Parquet footer pruning and the scan guard
│   │   ├── chunks.py       tiles the remote corpus into chunks from metadata
│   │   ├── profile.py      cheap per-chunk aggregates and cluster sampling
│   │   ├── promotion.py    index a chunk, digest it, drop it (teardown always)
│   │   ├── external.py     the in-place inverted index over remote Parquet
│   │   ├── templates.py    template extraction and template-level embeddings
│   │   └── loghub.py       loghub loaders (BGL), raw-line and structured-CSV
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
│       ├── sweep.py      the four-pass sweep (map/profile/sample/promote)
│       ├── triage.py     chunk scoring and sampling uncertainty
│       ├── cost.py       off-model query cost and timing notes
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
    ├── chat_box/         type-ahead chat input (custom component, no build)
    └── new_chat/         role-selection page
skills/
└── log-investigation/SKILL.md  hypothesis workflow for the evidence stage
```

## MCP tool service

The optional standalone MCP service owns SereneDB access, auto-complete, and the
investigation skills. Start it separately from the backend:

```bash
bash ./mcp_server.sh                 # Streamable HTTP, default 127.0.0.1:8010/mcp
TOOLS_TRANSPORT=mcp bash ./backend.sh
```

The backend defaults to `TOOLS_TRANSPORT=local`. Set it to `mcp` to route the
seven evidence tools and `/auto-complete` through MCP; if the service is
unreachable, the backend logs the failure and falls back to the in-process DB
implementation. MCP mode also exposes `list_skills` and `read_skill`, while
local mode keeps the sandboxed `skills/` filesystem backend. The server exposes
skills both as MCP resources (`skill://log-investigation`) and tools for clients
such as MCP Inspector, Kiro, or Claude Desktop.

MCP configuration: `MCP_HOST`, `MCP_PORT`, `MCP_PATH`, `MCP_URL`,
`MCP_CALL_TIMEOUT_S`, and `MCP_SKILLS_ROOT`. Loopback is the default. Set
`MCP_AUTH_TOKEN` before binding `MCP_HOST` to a non-loopback address; the server
refuses an unauthenticated non-loopback configuration. FastMCP is pinned in
`pyproject.toml`; regenerate `requirements.txt` with `uv export` after changing
dependencies.

### Connecting an external AI agent

Any MCP-capable agent can add this service to its tool set. Start the MCP server
first, then register the Streamable HTTP endpoint in the agent's MCP settings:

```text
Endpoint: http://127.0.0.1:8010/mcp
Transport: Streamable HTTP
```

For a non-loopback server, use the configured HTTPS URL or hostname and send the
bearer token from `MCP_AUTH_TOKEN` as an HTTP header:

```text
Authorization: Bearer <MCP_AUTH_TOKEN>
```

The exact registration command differs between Claude Code, Codex, Kiro,
Claude Desktop, MCP Inspector, and other clients. Use the client's
**Streamable HTTP / remote MCP server** option; do not configure this endpoint as
an stdio subprocess. For MCP clients that accept a JSON server definition, the
equivalent configuration is:

```json
{
  "name": "ai-chatbot-serenedb",
  "transport": "streamable-http",
  "url": "http://127.0.0.1:8010/mcp",
  "headers": {
    "Authorization": "Bearer <MCP_AUTH_TOKEN>"
  }
}
```

The `headers` entry is only needed when `MCP_AUTH_TOKEN` is configured. Keep the
actual token out of committed configuration files. Loopback development is
unauthenticated by default; binding the server to a non-loopback address
requires `MCP_AUTH_TOKEN`.

After connecting, the agent should list the available MCP tools and use them as
its evidence-gathering arsenal rather than inventing SQL or relying on
unsupported log facts:

1. Use `list_skills` and then `read_skill("log-investigation")` when the request
   asks why a failure occurs, asks for root-cause analysis, or requires testable
   hypotheses.
2. Use `search_logs_tool` for semantic or message-content searches. Use
   `get_log_by_line_id_tool` when the user supplies a `Line ID: n` reference.
3. Use `get_data_tool` to discover known services, pids, or event ids before
   requesting a trend for an exact target with `get_trend_tool`.
4. Use `count_by_category_tool`, `top_in_category_by_service_tool`, and
   `compare_periods_tool` for computed counts, rankings, and period comparisons.
5. Use `auto_complete` only for selector-prefixed suggestions (`@` service, `#`
   event id, `^` event template, `!` pid, `$` content).
6. Ground the final answer in returned rows and aggregates. Preserve the tool
   result identifiers and distinguish an empty result from a failed tool call.

The external agent connects directly to the MCP service, so the backend does
not need `TOOLS_TRANSPORT=mcp` for that agent to use the tools. Set
`TOOLS_TRANSPORT=mcp` only when the chatbot's own LangGraph pipeline should use
the same MCP service instead of its local fallback implementation.

## License

The code is released under the [MIT License](LICENSE).

The OpenStack sample in `src/backend/data/` (`OpenStack_2k.log_structured*.csv`)
comes from [Loghub](https://github.com/logpai/loghub) and is not covered by the
MIT license: it is redistributed under the Loghub license, which permits research
or academic use and requires attribution. See
[`src/backend/data/LICENSE-loghub`](src/backend/data/LICENSE-loghub). The BGL
corpus is not bundled; it is loaded from a local Loghub download
(`BGL_LOG_PATH`) under the same terms.

If you use the Loghub data, cite: Jieming Zhu, Shilin He, Pinjia He, Jinyang
Liu, Michael R. Lyu. *Loghub: A Large Collection of System Log Datasets for
AI-driven Log Analytics*. ISSRE, 2023.
