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

The `scope` stage uses five independent TypeSafe (Jev) Noul judgments: whether
`latest_message` is a log request, whether the raw message transcript names a
queryable target, whether the user authorized proceeding without clarification,
whether new log evidence is needed, and whether the request is a dataset-wide
overview. Code owns the policy: non-log requests go to a general reply; vague log
requests are clarified unless authorized, out of clarification budget, or (for
the team lead and manager only) an overview of the whole dataset; otherwise `needs_new_evidence` selects a direct answer or
investigation. The **evidence stage** (`find_evidence`) is a deep agent: it plans the
investigation and calls the database tools itself, then submits a free-text summary
of its findings. The report of `summary` plus `evidence` (and optional
`hypotheses`) is assembled off-agent: code builds one evidence item per tool call
(name, arguments, success or failure, linked results, so failed and empty calls
show too), and the model only writes the summary and at most three hypotheses
citing those items. Results are numbered `R1..Rn` for the answer tables. The
answer model sees every row of a measured result (counts, trends, up to 100 rows)
and a 10-row sample of example results; each result states how many rows are
shown. Each role's answer rules live only in its skill. A bundled `log-investigation` skill
decides when hypotheses apply.

## One question, three roles

The same agent and corpus answer every role, but each role gets different
**questions, evidence and next steps** (not just a different length):

| Role | Asks | Evidence collected | Answer ends with |
| --- | --- | --- | --- |
| Engineer | What failed, where, and what happened just before and after? | full-text search → nearby events (approximated until the nearby-events tool exists) → an isolated-or-recurring count | a possible cause, labelled a hypothesis, and one next diagnostic check |
| Team lead | Which problems recur, how often, which components/teams? | counts by event and component over a stated period, a trend for the top patterns, owning teams from the ownership table | what the team should investigate first, and why |
| Manager | What deserves attention first, and what happens next? | measured frequency, severity share, trend direction and breadth, combined by a deterministic priority score | at most three ranked priorities, each with an action and owning team; no invented cost, downtime or customer impact |

The evidence plan for each role is a skill (`skills/engineer-investigation`,
`skills/recurring-patterns`, `skills/risk-prioritization`) that is injected into
the evidence agent's prompt and also served over MCP, so Claude connected to the
MCP server follows the same plans. The role never reaches the TypeSafe
classifiers: code decides, for example, that a dataset-wide overview is a valid
starting point for the team lead and manager but not for the engineer.

Every answer shows its work. Code (not the model) appends:

- **Findings** labelled *Measured* (cited to query results), *Hypotheses* and
  *Limitations*; a "measured" claim without a valid cited result is downgraded to
  a hypothesis, and claims backed only by example search hits are flagged as not
  a frequency.
- **Recommended next steps** with their reason and owning team.
- **Data scope and queries**: datasets, time window, filters, which results are
  measured counts and which are examples, whether each result was truncated, and
  the executed SQL for every result.

Counts are log entries, never "incidents". The priority score is computed from
the measured counts (never by a model) and shown with its formula:
`0.4·volume_share + 0.3·error_share + 0.2·trend_up + 0.1·breadth`.

### Tool results

Every evidence tool returns one envelope: `status`, `operation`, `kind`
(`search | context | aggregate | trend | lookup | catalog | test`), `dataset`,
the executed `query`, `filters`, `limit`, `returned`, `total_matches`,
`truncated` and `data`. Only `aggregate`/`trend` results are frequencies;
`search` results are examples. The same envelope is returned by the in-process
tools and by the MCP server (`src/backend/pipeline/tools/envelope.py`). A tool
that omits a field is reported as "unknown", never guessed.

### Answering as another role

Under each answer, the "As <role>" buttons run a **new investigation** for that
role (its own evidence plan, not a rewording) in the background and cache it per
message and role: switching manager → engineer → manager never runs the same
investigation twice. A request at a higher effort than the cached run re-runs.
`POST /chats/{id}/messages/{seq}/role-answer` and `GET /chats/{id}/role-answers`
back the buttons; `/chat/reframe` remains for compatibility.

### Worked investigation

`bash ./worked_investigation.sh --topic "<issue or pattern>"` asks the same topic
as engineer, team lead and manager against the live database and writes
`docs/worked_investigation.md`: per role the question, route, tool calls,
executed queries, key rows, evidence report, answer and recommended action, plus
what the data does and does not establish. It needs a seeded SereneDB and real
provider keys, and it also writes the raw runs to
`.data/worked_investigation_runs.json`.

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
| `openstack` | local table | 207,820 | BM25, exact counts, templates, auto-complete, record id | default; three OpenStack nova runs (two normal, one with failures) in one table |
| `bgl` | local table | 4,747,963 | BM25, exact counts, templates, auto-complete, record id | Blue Gene/L RAS log; spans seven months, so it answers long-range questions |
| `otel_s3` | remote Parquet | 1,000,000,000 | approximate aggregates, chunk sweep, templates | the corpus itself, read in place |
| `otel_slice` | **in-place index** | — | BM25, exact counts | text search over the remote corpus |

Because builders read the registry rather than hard-coded SQL, adding BGL and the
loghub-loaded OpenStack corpus required **no new SQL in any builder**. Both local
corpora are loaded by one loader (`src/backend/db/loghub.py`) with one parser
each (`parse_bgl_line`, `parse_openstack_line`) and share loghub's column naming
(`LineId`, `Level`, `Component`, `Content`, `EventTemplate`, `Timestamp`).

Capabilities are declared, not assumed, and a missing one is refused at
registration rather than failing at query time. `otel_slice` deliberately
declares neither `templates` nor `auto_complete` nor `record_id`: all three
needed columns that only existed while rows were being copied locally, and that
copy step no longer exists.

### Three tiers

**Tier A — local, indexed, exact.** `openstack` and `bgl`. Millisecond searches
and exact counts. Both carry an `EventTemplate` derived at load time, and both are
loaded with `--no-embed`, so search is lexical (BM25) for now. Template-level
embeddings remain available as an opt-in step (`backend.db.templates`): for BGL,
about 20,352 distinct templates stand in for 4.7M rows, which is what makes
semantic search affordable at that size (embedding every row would take days;
embedding the templates took 36 minutes).

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
consumes one of the 5–21 calls the effort level permits. `max_promotions` is the
existing per-effort dial, so it governs this too:

| Effort | Tool calls | Rows scanned | Promotions |
| --- | --- | --- | --- |
| Low | 5 | 25M | 0 |
| Medium | 9 | 100M | 0 |
| High | 11 | 250M | 0 |
| Extreme | 17 | 1B | 2 |
| Brutal | 21 | 5B | 4 |

The whole sweep runs **inside a single tool call**, because 1,624 chunks cannot
be 1,624 calls when the ceiling is 21. The agent decides *whether* to sweep and,
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

Measured before this change: **380.5 MiB of the 3 GiB budget**, holding the full
4.7M-row BGL corpus (281 MiB) with all 20,352 of its template embeddings (64 MB).
The OpenStack corpus is estimated at ~32 MB (207,820 rows); the loader checks the
budget before building and reports the real size afterwards.

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
  `nomic-embed-text:v1.5` -- only for the optional template embeddings;
  `bash ./setup.sh` loads with `--no-embed` and needs no embedding model
- The raw loghub logs in `src/backend/data/` (gitignored): the three OpenStack
  nova runs `openstack_normal1.log`, `openstack_normal2.log`,
  `openstack_abnormal.log`, and `BGL.log` (743 MB; `BGL_LOG_PATH` may point
  elsewhere). See Setup.
- Nothing extra for the remote OTel corpus: it is read from public S3 in place.

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
| `SERENEDB_HOST` / `SERENEDB_PORT` / `SERENEDB_DB` / `SERENEDB_USER` / `SERENEDB_PASSWORD` | `localhost` / `7890` / `postgres` / `postgres` / `postgres` | SereneDB target (with Docker the host port is the mapped one, see `docker ps`, and can change when the container is recreated) |
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

The chat box is locked while a run is active: the text field is disabled and the
send button becomes a **stop button** that calls the cancel endpoint. The box shows
"Stopping…" until the run reports `cancelled` (the worker checks for a cancel
between events), then unlocks. A stopped run saves no answer, so the question that
started it is flagged `stopped` in the store. The UI shows "Stopped by user" under
it (also after a refresh), and the agents read the turn labelled
`[investigation stopped by the user; no answer was given]`, so a later "what was my
whole question again?" is answered from the conversation instead of re-running the
investigation. Role-switch runs are not affected by the stop button. A run that
fails unlocks the box too, and its error stays visible under the question.

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
and picking a suggestion replaces the draft words it spans (a multi-word event
template such as `Removable base files: <*>` replaces every word it lines up with,
so nothing is duplicated).

The chat box sends no dataset, so every local corpus loaded from a `.log` file
(`openstack` and `bgl`) is searched and the results are merged: a value found in
both appears once with its counts summed. The remote OTel tiers are never
queried, so a keystroke cannot reach object storage.

A leading **selector** scopes the lookup to one field. One character is assigned
per field, chosen so that it occurs in *no* indexed value and can therefore
never collide with real text:

| Selector | Field | Column (both corpora) |
| --- | --- | --- |
| `@` | service | `Component` |
| `^` | event template | `EventTemplate` |
| `$` | message content | `Content` |

So `@nova` suggests OpenStack components (and `@KERN` BGL ones), `^Took` suggests
event templates, and `$timeout` suggests log lines whose content contains
`timeout`. (`Content` is word-indexed, so it is matched as whole lines rather
than single words.) There is no pid/node or event-id selector.

Each dropdown entry shows its **count** as `Count: n`. A `$` content entry instead
shows `Line ID: n (dataset)`, and picking it inserts that reference into the box
rather than pasting the whole message; the dataset is included because a line id
is only unique within one corpus, and the agent passes both to
`get_log_by_line_id_tool`. A bare selector lists that field's most popular
values. A selector is **required**: a word without one is not looked up and
suggests nothing. The selector is stripped before matching and is shown as a prefix
on each dropdown entry (a display-only reminder of the field in scope); it is
consumed when a suggestion is picked, so the bare value is inserted.

Matching is always case-insensitive, and by default ranking is too — a value is
never preferred just for matching the case you typed. Typing `!` immediately after
the selector (`@!nova`) switches to a case-sensitive ranking, putting values whose
case matches what you typed first. `!` is only this modifier, never a selector.

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
uv sync           # install dependencies and the project itself
bash ./setup.sh   # load the local log corpora (no arguments)
```

`setup.sh` loads both local corpora through the loghub loader
(`backend.db.loghub`), in this order, each with `--no-embed`:

1. `openstack` -- one table from the three nova logs, read one after another:
   `src/backend/data/openstack_normal1.log`, `openstack_normal2.log`,
   `openstack_abnormal.log` (207,820 lines). `LineId` runs on across the files,
   so it is unique in the table; nothing records which run a row came from.
2. `bgl` -- `src/backend/data/BGL.log` (or the file named by `BGL_LOG_PATH`),
   4,747,963 lines. This takes a long time.

Each run drops and rebuilds the table, and the disk budget is checked before
anything is built. The script stops at the first failure, including a missing
source file, with the loader's message naming what to add. Timestamps are stored
as `TIMESTAMPTZ` in UTC and every database session is pinned to UTC, so a time
window means the same thing on every machine.

To load one corpus, or loghub's 2,000-line structured samples instead (these
replace the same table):

```bash
uv run python -m backend.db.loghub openstack --no-embed
uv run python -m backend.db.loghub openstack_2k --no-embed   # src/backend/data/OpenStack_2k.log_structured.csv
uv run python -m backend.db.loghub bgl_2k --no-embed         # src/backend/data/BGL_2k.log_structured.csv
```

Dropping `--no-embed` also builds template-level embeddings, which needs the
Ollama embedding model from Prerequisites.

### Team ownership (fictional)

`src/backend/data/team_ownership.csv` is a small, **invented** mapping from log
components to the teams that "own" them, used so team-lead and manager answers
can group counts by team. It is not real organisational data. Columns:

| Column | Meaning |
| --- | --- |
| `dataset` | Base-corpus dataset (`OpenStack` or `BGL`) |
| `component` | Exact value of the corpus's `Component` column (the registry's `service` field) |
| `team` | Owning team |

| Team | Owns |
| --- | --- |
| API Gateway | `nova.osapi_compute.wsgi.server`, `nova.api.openstack.*`, `nova.metadata.wsgi.server` |
| Compute Platform | `nova.compute.manager`, `nova.compute.claims`, `nova.compute.resource_tracker` |
| Virtualization | `nova.virt.libvirt.driver`, `nova.virt.libvirt.imagecache` |
| Scheduling | `nova.scheduler.*` |
| BG/L Kernel | BGL `KERNEL`, `APP` |
| BG/L Control System | BGL `MMCS`, `BGLMASTER`, `CMCS` |
| Hardware Ops | BGL `MONITOR`, `HARDWARE`, `LINKCARD`, `DISCOVERY` |
| Network Ops | BGL `SERV_NET` |

Components that are not listed count as team `unowned`, so a team total never
silently drops rows. `backend.db.ownership` validates the file (header, no empty
fields, no component mapped twice); `uv run python -m backend.db.ownership`
loads it into SereneDB as `team_ownership` (no log table is touched). Today the
prompts read the CSV directly to name teams; a team-count tool would join
`Component = team_ownership.component`. Replace the CSV if
the organisers' ownership table becomes available; keep the three columns.

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

The suite runs without external services; the gated live corpus, route and
confidence tests below are skipped unless enabled.

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
| `test_chat_box_stop.py` / `test_chat_stop_ui.py` / `test_stopped_turns.py` | chat box lock and stop button, stopped/failed runs in the UI, the stored `stopped` flag and the transcript label |
| `test_decision_nodes.py` / `test_confidence_prompts.py` / `test_live_confidence.py` / `test_find_evidence.py` | scope & confidence decisions, atomic classifier payloads, gated live confidence checks, evidence-node wiring |
| `test_stream_projection.py` / `test_tool_progress.py` / `test_answer_rendering.py` | streaming projection, tool-progress events, serializer↔renderer sync |
| `test_stream_events.py` | the shared event reducer: backend-saved and live progress agree, refusals never saved as failures, malformed payloads |
| `test_tool_envelope.py` / `test_data_scope.py` / `test_ownership.py` | tool-result contract and query capture, code-built data scope, ownership table |
| `test_evidence_builder.py` / `test_answer_catalog.py` | code-built evidence items and tool calls, the answer prompt's result catalog |
| `test_role_playbooks.py` / `test_answer_ledger.py` / `test_worked_investigation.py` | role objectives and playbooks, findings ledger and priority score, document generator |
| `test_role_switch.py` / `test_role_switch_ui.py` | per-role answer cache, role-switch endpoint and buttons |
| `test_query_layer.py` / `test_budget.py` / `test_search_terms.py` | dataset registry, bounded/metered queries, disk budget, tokenizer |
| `test_datasets.py` / `test_trend_compare_queries.py` | registrations (incl. the `openstack` table and selectors), trend and period-comparison query shapes |
| `test_auto_complete.py` / `test_session_time_zone.py` | type-ahead merged across `openstack` + `bgl` and the chat-box selector gate; every DB session pinned to UTC |
| `test_registry.py` / `test_mcp.py` | local↔MCP tool parity (nine tools), MCP server validation (no auto-complete tool) |
| `test_templates.py` / `test_template_build.py` | template extraction, and that the embedding loop provably terminates |
| `test_openstack.py` / `test_openstack_live.py` | OpenStack registration, the nova-log parser and multi-file loader; gated live checks against the loaded 207,820-row corpus |
| `test_bgl.py` / `test_bgl_live.py` | BGL registration and parser; gated live checks against the full 4.7M-row corpus |
| `test_sampling_and_external.py` / `test_promotion.py` | cluster-sample uncertainty, in-place index plans, promote/digest/teardown |
| `test_sweep*.py` (4 files) | sweep orchestration, effort gating, observability trace, scoring |
| `test_otel_live.py` | gated live checks against the remote corpus and its in-place index |

> The offline suite reports `5 failed, 671 passed, 46 skipped`. Three failures
> are known and pre-existing, all in `tests/test_rendering.py` (inline-marker
> rendering edge cases). The other two,
> `test_bgl.py::test_an_uppercase_severity_is_refused_for_otel` and
> `test_sampling_and_external.py::test_the_sample_query_tags_every_window`, need a
> reachable SereneDB although they are not gated, and pass when it is. The skips
> are the gated live cases below. The suite passes identically under `TOOLS_TRANSPORT=local` and
> `TOOLS_TRANSPORT=mcp`.

### Live corpus tests

Three suites run against the real corpora and are skipped unless gated, because
each costs real queries against a live database (and, for OTel, public S3):

```bash
RUN_OPENSTACK_LIVE=1 uv run pytest tests/test_openstack_live.py   # 207,820-row OpenStack
RUN_BGL_LIVE=1       uv run pytest tests/test_bgl_live.py         # full 4.7M-row BGL
RUN_OTEL_LIVE=1      uv run pytest tests/test_otel_live.py        # remote corpus + index
```

`RUN_OPENSTACK_LIVE` and `RUN_BGL_LIVE` need the corpus loaded (`bash ./setup.sh`;
BGL reads `BGL_LOG_PATH` when set); each skips with a message when its table is
absent. The template-embedding checks in `test_bgl_live.py` skip until templates
are embedded. `RUN_OTEL_LIVE` needs only network access; the tests that require
the in-place index skip themselves with a message when it has not been built,
rather than failing.

### Live route tests

`bash ./routes.sh` replays each scripted conversation in
`tests/test_live_scope_routes.py` against the real normalizer and scope
classifier and asserts the route each turn takes (`clarify`, `general`,
`direct_answer`, `find_evidence`). It is gated by `RUN_LIVE_ROUTES=1` (the script
sets it) and must be run alone — never inside the full suite, because
`tests/conftest.py` installs dummy provider keys before the backend's
import-time clients are built. Each gated run writes
`.data/scope_routes_report.html` (gitignored; override with
`RUN_LIVE_ROUTES_REPORT`) and prints its path once; the per-turn numbers appear in
the assertion failure. Offline, only a case-data sanity check runs. Costs two
live API calls per asserted turn, and the classifier is stochastic — re-run
before treating a flipped route as a regression. The scope prompt's
`QUERYABLE_TARGETS` no longer lists an event id. Each scripted conversation sets
the user's role: an overview such as "what recurring problem do we have" is
scoped for team lead and manager but still clarified for the engineer.

### Live confidence checks

`bash ./confidence.sh` replays representative evidence and answer states through
the real confidence classifier and checks the code-owned revision policy. It is
gated by `RUN_LIVE_CONFIDENCE=1`, must run alone, and writes
`.data/confidence_report.html` (override with `RUN_LIVE_CONFIDENCE_REPORT`). The
report includes each atomic Noul signal and the selected policy route. Thresholds
are starting values validated against these cases; the cases are stochastic and
must not be re-baselined to match a flipped result.

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
must start with a selector scoping it to one field (`@` service/component, `^`
event template, `$` content; a `!` right after the selector ranks
case-sensitively); a bare selector lists that field's most popular values, while
a query with no leading selector returns no suggestions. Every local `.log`
corpus (`openstack`, `bgl`) is always searched and the suggestions merged; there
is no `dataset` parameter.
Returns `{"suggestions": [{"suggestion", "count"}, ...]}`; a `$` content
suggestion carries `line_id` and `dataset` in place of `count`.

### Chats

| Endpoint | Result |
| --- | --- |
| `GET /chats` | `{chats: [{id, role, title, created_at, updated_at}]}` |
| `POST /chats` | create `{chat_id, role, title?}` → chat record |
| `DELETE /chats/{chat_id}` | `{deleted: bool}` |
| `GET /chats/{chat_id}/messages` | `{messages: [{seq, role, content, tool_results, progress, reframed_for, stopped, created_at}]}` (`stopped`: the user stopped the investigation of this question) |
| `POST /chats/{chat_id}/messages` | append `{role, content, tool_results?, progress?, reframed_for?}` |

### Background investigations

| Endpoint | Result |
| --- | --- |
| `POST /chats/{chat_id}/investigate` | persist the user turn and start a run: `{role, effort, question, history}` → `{run_id, status}` |
| `GET /chats/{chat_id}/run/{run_id}` | `?since=N&wait=S` → `{status, events, next_since, error, result}` |
| `GET /chats/{chat_id}/active-run` | `{run_id}` for an in-flight run, else `{run_id: null}` |
| `POST /chats/{chat_id}/run/{run_id}/cancel` | `{cancelled: bool}` |

`status` is `running | done | error | cancelled`. On `done`, `result` carries
`{answer, tool_results, progress, elapsed_seconds}` — the answer payload the
backend persists as the assistant message, plus the run's own wall-clock window
from accepting the question to finishing (the UI shows it under the answer).
`wait` long-polls (capped at 30s). Starting an
investigation for a chat that already has one returns the existing `run_id`.
`DELETE /chats/{chat_id}` cancels and discards any active run.

### Role switch

| Endpoint | Result |
| --- | --- |
| `POST /chats/{chat_id}/messages/{seq}/role-answer` | `{role, effort}` → `{status: "cached", answer}` or `{status: "running", run_id}` (poll the run endpoint) |
| `GET /chats/{chat_id}/role-answers` | `{answers: [{message_seq, role, effort, content, tool_results, progress}]}` |

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
worked_investigation.sh  one topic as all three roles -> docs/worked_investigation.md
confidence.sh gated live confidence-policy tests (RUN_LIVE_CONFIDENCE=1) + HTML report
setup.sh      load the local corpora via loghub: openstack, then bgl (--no-embed; no arguments)
src/
├── stream_events/       one event reducer shared by backend/jobs.py (saved progress) and the UI (live)
├── backend/
│   ├── api.py            FastAPI endpoints, streaming, and admission control
│   ├── jobs.py           in-process background investigation registry
│   ├── observability.py  request-correlation middleware and metrics registry
│   ├── appdb/            users, sessions, chats, messages (SQLite)
│   ├── data/             raw loghub .log files (gitignored) + the 2k structured CSV samples
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
│   │   └── loghub.py       loghub loaders: BGL and OpenStack parsers, raw-line and structured-CSV
│   └── pipeline/
│       ├── graph.py      LangGraph nodes, wiring, and checkpointer selection
│       ├── state.py      pipeline state schema
│       ├── contracts.py  Pydantic agent response models
│       ├── answers.py    structured-answer → marker text serialization
│       ├── roles.py      role objectives and role -> playbook skill map
│       ├── playbooks.py  playbook text and ownership mapping injected into prompts
│       ├── priority.py   deterministic priority score from measured counts
│       ├── data_scope.py data scope derived from tool results
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
├── log-investigation/SKILL.md      hypothesis workflow for the evidence stage
├── engineer-investigation/SKILL.md role playbook: error lines, nearby events, next check
├── recurring-patterns/SKILL.md     role playbook: counts, trends, teams
└── risk-prioritization/SKILL.md    role playbook: ranked priorities and actions
```

## MCP tool service

The optional standalone MCP service owns SereneDB access for the evidence tools
and the investigation skills. Start it separately from the backend:

```bash
bash ./mcp_server.sh                 # Streamable HTTP, default 127.0.0.1:8010/mcp
TOOLS_TRANSPORT=mcp bash ./backend.sh
```

The backend defaults to `TOOLS_TRANSPORT=local`. Set it to `mcp` to route the
seven bounded query tools and the two sweep tools through MCP; if the service is
unreachable, the backend logs the failure and falls back to the in-process DB
implementation. `/auto-complete` is not an MCP tool: it is a chat-box feature and
is always answered in-process, whatever the transport. MCP mode also exposes `list_skills` and `read_skill`, while
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
2. Pass a `dataset` id (`openstack` is the default; `bgl`, `otel_s3`,
   `otel_slice`) to every log tool; there are no table names. Use
   `search_logs_tool` for message-content searches on an indexed corpus. Use
   `get_log_by_line_id_tool` when the user supplies a `Line ID: n (dataset)`
   reference, passing both `line_id` and `dataset`.
3. Use `get_data_tool` to discover a field's real values (services, entity ids,
   templates) before requesting a trend for an exact target with `get_trend_tool`.
4. Use `count_by_category_tool`, `top_in_category_by_service_tool`, and
   `compare_periods_tool` for computed counts, rankings, and period comparisons;
   `sweep_chunks_tool` and `investigate_chunk_tool` cover the remote corpus at
   high effort (price a sweep first with `confirm_cost=false`).
5. Ground the final answer in returned rows and aggregates. Preserve the tool
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
