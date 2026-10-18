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
investigation. The clarifier sees the same dataset catalog the classifier judged
against and is told which gate failed. The second round is a fixed question built
in code from the registry: one choice per corpus (openstack, bgl, OTel -- `otel_s3`
and `otel_slice` are two views of the OTel corpus, and the agent picks between
them), or all of them. Once the
budget is spent, a reply that needs new evidence is investigated over every local
corpus and the answer opens with an "Assumed scope" line; one that does not (such as
"what?") gets a direct answer without the role's evidence sections. The **evidence stage** (`find_evidence`) is a deep agent: it plans the
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
| Engineer | What failed, where, and what happened just before and after? | full-text search → nearby events around the hit (`get_nearby_events_tool`, optionally the same process/node or request) → an isolated-or-recurring count | a possible cause, labelled a hypothesis, and one next diagnostic check |
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
  *Limitations*; a "measured" claim without a valid cited result, or with a number
  that the cited results do not show, is downgraded to a hypothesis, and claims
  backed only by example search hits are flagged as not a frequency.
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
`template`, `record_id`, `request_id`) plus the capabilities it can honestly
honour. A field is declared only where the corpus really has it: `request_id`
exists only on OpenStack, derived at query time from the `ADDR` column (no
reload needed). Query
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

The evidence agent has fourteen tools. One is the dataset catalog
(`list_datasets_tool`: per dataset, rows, first and last timestamp, severity
levels and a window covering every row, measured on each call, so windows come
from the data rather than a guess). One counts each whole local dataset by
severity in a single query (`count_by_dataset_tool`, no time window), and one
counts local rows by owning team and severity (`count_by_team_tool`, joined to
the real `team_ownership` table). Eight are ordinary bounded queries; one
manages the OTel index (`otel_index_tool`); two are the sweep
(`sweep_chunks_tool`, `investigate_chunk_tool`).

One of the bounded queries, `get_nearby_events_tool`, gives the engineer the
lines just before and after a search hit (`before`/`after`, 0-50 each), in
record-id order with an `offset` from the anchor. `same_entity` keeps only the
anchor's process (OpenStack) or node (BGL); `same_request` keeps only its
request id and works only on OpenStack, the one corpus that records one (it is
refused elsewhere). An unknown line id is an empty success with a note. The rows
are examples (`kind=context`), never a frequency.

`otel_index_tool` manages the in-place OTel index, and in normal use the agent
never has to. The index covers one window at a time (recorded in its build
record), so `search_logs_tool` on `otel_slice` indexes the window it was given
whenever the current index does not already cover it: a search there either
answers or explains itself, and never fails for a missing index. There is no
effort gate and no cost confirmation — a search that cannot be answered is worth
less than the minutes the build costs. A window already covered costs nothing,
and builds are serialized so two concurrent searches cannot index at once. The
tool itself reports coverage (`exists`, `window_start`, `window_end`,
`severities`, `index_bytes`) with no arguments, and indexes a window when given
one, which is useful before a series of searches. The one unanswerable case is a
search with no window when no index exists yet: that is refused, because the
request does not say what to index.

**Disk is held for one index, not two.** A build drops the previous index before
creating the new one, so the budget credits those bytes back rather than
charging for both at once -- otherwise a replacement could be refused over
budget for space it was about to free. The size is measured at build time and
kept in the build record (SereneDB will not size an external index on request),
which is what makes the credit possible and what `index_bytes` reports.
`otel_index_tool` with `release=true` drops the index and returns its disk,
measured: a one-second window indexed 87,650 rows into **5.1 MiB**, and
releasing it took the database from 423.6 MiB back to 418.6 MiB. Release only
when the corpus is finished with -- the next uncovered search rebuilds.

**A build holds the server to the budgeted memory ceiling.** `memory_limit` is
global, does not survive a container restart, and a restarted SereneDB came back
at **5.7 GiB** -- the value that originally caused three OOM kills. Two of three
builds in one session were killed mid-`CREATE INDEX`, each time taking the
instance down and changing its host port. A build now lowers the ceiling to the
budgeted 512 MiB first (reported as `488.2 MiB`) unless it is already at or
below it, which turns an out-of-memory *crash* into a failed query. With the
ceiling held, the same build completed in **446.7s** and again in **444.6s**.

The sweep tools are **withheld entirely below Extreme effort** rather than
offered and refused — a visible tool gets tried eventually, and a refusal still
consumes one of the 6–22 calls the effort level permits. `max_promotions` is the
existing per-effort dial, so it governs this too:

| Effort | Tool calls | Rows scanned | Promotions |
| --- | --- | --- | --- |
| Low | 6 | 25M | 0 |
| Medium | 10 | 100M | 0 |
| High | 12 | 250M | 0 |
| Extreme | 18 | 1B | 2 |
| Brutal | 22 | 5B | 4 |

The whole sweep runs **inside a single tool call**, because 1,624 chunks cannot
be 1,624 calls when the ceiling is 22. The agent decides *whether* to sweep and,
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

`bash ./bootstrap.sh` installs and checks all of this; the list is what it
needs, and what it will offer to install.

- A Linux host with `curl` and `python3` (the SereneDB installer is Linux-only;
  elsewhere use `bash ./bootstrap.sh --db docker`)
- Python 3.14+ and [uv](https://docs.astral.sh/uv/) -- bootstrap installs uv if
  it is missing, and uv provides the Python
- A SereneDB instance: bootstrap starts one as a native binary (`serened`, no
  root) or in Docker, or uses the one `SERENEDB_*` already points at. It speaks
  the PostgreSQL wire protocol on port 7890 and needs no credentials by
  default
- An embedding provider the **database** can reach -- only for the optional
  template embeddings; `bash ./setup.sh` loads with `--no-embed` and needs no
  embedding model. `ai_embed` runs inside SereneDB and resolves its endpoint
  and credentials from the server-side secret the calls name
  (`EMBEDDING_SECRET`, default `gemini`), so the host's own reachability is
  irrelevant and `MODEL_BASE_URL` does not change where embeddings come from.
  See **Changing the embedding model**.
- The raw loghub logs in `src/backend/data/` (gitignored): the three OpenStack
  nova runs `openstack_normal1.log`, `openstack_normal2.log`,
  `openstack_abnormal.log`, and `BGL.log` (743 MB; `BGL_LOG_PATH` may point
  elsewhere). These are **optional**: bootstrap falls back to the bundled
  2,000-row loghub samples for whichever corpus is missing, and says so. See
  Setup.
- Nothing extra for the remote OTel corpus: it is read from public S3 in place.

## Environment

`.env.example` is the annotated template; `bash ./bootstrap.sh` copies it to
`.env` and names any key still unset. Create it by hand with `cp .env.example
.env`, and fill in the keys for the providers in use:

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
bash ./bootstrap.sh          # everything: uv, deps, server, secret, corpora
```

That is the whole setup on a Linux host. It runs eight steps, each of which it
skips when already done, so **re-running it is the way to recover an instance**
after a reboot or a recreated container:

| Step | What it does | Skipped when |
| --- | --- | --- |
| 1 | installs [uv](https://docs.astral.sh/uv/) | `uv` is on PATH |
| 2 | `uv sync` | never (it is already incremental) |
| 3 | creates `.env` from `.env.example` and reports unset keys | `.env` exists |
| 4 | starts SereneDB and writes `SERENEDB_PORT` to `.env` | a server already answers |
| 5 | waits for it to answer a *query*, not just accept a socket | – |
| 6 | holds the memory ceiling, creates the embedding secret | – |
| 7 | loads the ownership table and the corpora | that corpus is already loaded |
| 8 | prints the resulting state, including what the model returns | – |

```bash
bash ./bootstrap.sh --db docker          # SereneDB in Docker (docker-compose.yml)
bash ./bootstrap.sh --db existing        # use the server SERENEDB_* points at
bash ./bootstrap.sh --samples            # loghub's 2,000-row samples, not the full corpora
bash ./bootstrap.sh --datasets openstack # only this corpus
bash ./bootstrap.sh --skip-data          # set the server up, load nothing
bash ./bootstrap.sh --embed              # also build template embeddings (spends provider quota)
bash ./bootstrap.sh --force-reload       # rebuild corpora that are already loaded
bash ./bootstrap.sh --yes                # never prompt (for CI or a non-interactive shell)
bash ./bootstrap.sh --no-probe           # skip the single embedding call in step 8
```

Two steps download and execute a vendor installer -- uv from `astral.sh` and
SereneDB from `linux.serenedb.com`. Both print the exact command and ask first
unless `--yes` is given; `--db existing` with uv already installed avoids them
entirely.

**Step 4 in each mode.** `--db native` installs `serened` (no root; it listens
on `127.0.0.1:7890` and keeps its data, including persistent secrets, in
`.data/serenedb`), writes its pid to `.data/serened.pid` and logs to
`.data/serened.log`. `--db docker` uses `docker-compose.yml`, which **pins the
host port** to `SERENEDB_PORT` and keeps a named volume, so the port stops
drifting when the container is recreated and the loaded corpora survive; it
falls back to `docker run` when `docker compose` is absent, and if the daemon
refuses the connection it says how to fix it and points at `--db native`.

**What step 6 does, and why it is not optional.** `ai_embed` runs inside
SereneDB and resolves its endpoint and key from a server-side secret, so no
host-side setting can replace it. The secret is created `PERSISTENT`, which is
what makes it survive a restart -- verified by stopping a server and cold-starting
it on the same data directory, after which `ai_embed` answered with no
re-creation. The step also lowers the server's global `memory_limit` to the
budgeted 512 MiB; a freshly started instance reports **5.7 GiB**, the
configuration that OOM-killed index builds.

The database-side steps are `backend.db.provision`, which is usable on its own:

```bash
uv run python -m backend.db.provision --wait --memory --secret --report
uv run python -m backend.db.provision --report --probe-embedding   # one provider call
uv run python -m backend.db.provision --json                       # the same state as JSON
```

### Loading corpora on their own

`setup.sh` is step 7 by itself. It loads the ownership table first, then the named corpora through the
loghub loader (`backend.db.loghub`). By default it passes `--no-embed`; passing
`--embed` opts into template embeddings. These flags affect **templates only**:
`--no-embed` does not create template vectors, while `--embed` embeds the
corpus's distinct log templates. Neither option embeds the content/message of
each individual log entry; per-log-content embeddings are never created by
`setup.sh`.

With no datasets named it loads these, in this order:

1. `team_ownership` -- `src/backend/data/team_ownership.csv`, (re)loaded by
   `setup.sh` before any corpus. This recreates only the ownership table and
   does not touch log tables.
2. `openstack` -- one table from the three nova logs, read one after another in
   chronological order: `src/backend/data/openstack_abnormal.log` (2017-05-14),
   `openstack_normal1.log`, `openstack_normal2.log` (2017-05-16 to 05-17), 207,820
   lines. `LineId` runs on across the files, so it is unique in the table and its
   order is time order; nothing records which run a row came from.
3. `bgl` -- `src/backend/data/BGL.log` (or the file named by `BGL_LOG_PATH`),
   4,747,963 lines. This takes a long time.

Each run drops and rebuilds the table, and the disk budget is checked before
anything is built. The script stops at the first failure, including a missing
source file, with the loader's message naming what to add. Timestamps are stored
as `TIMESTAMPTZ` in UTC and every database session is pinned to UTC, so a time
window means the same thing on every machine.

To load one corpus, or loghub's 2,000-line structured samples instead (these
replace the same table), name the datasets -- it is the same loader, so
everything goes through `setup.sh`:

```bash
bash ./setup.sh openstack            # one corpus
bash ./setup.sh openstack_2k         # src/backend/data/OpenStack_2k.log_structured.csv
bash ./setup.sh bgl_2k               # src/backend/data/BGL_2k.log_structured.csv
bash ./setup.sh --embed openstack    # also build template embeddings
```

`--embed` builds template-level embeddings, which needs the embedding service
from Prerequisites.

### Changing the embedding model

`EMBEDDING_MODEL` and `EMBEDDING_DIMENSION` must agree with each other and with
what the database's endpoint really returns, and a change invalidates the
vectors already stored. Three things break silently or obscurely otherwise, so
`backend.db.templates.embedding_compatibility(dataset)` checks all three and
`build_templates` refuses rather than spending the build:

Three settings have to agree, and `backend.db.templates.embedding_compatibility(dataset)`
checks each against the live database (`problems` carries a stable `code`):

| Problem | How it shows | Fix |
| --- | --- | --- |
| `cannot_embed` / `missing_secret` | `ai_embed: secret '<name>' not found` -- a recreated container loses its secrets | create the secret (below) |
| `declared_dimension` | the table stores `FLOAT[EMBEDDING_DIMENSION]` and the call casts to it, so a wider vector is **truncated**, not refused | set `EMBEDDING_DIMENSION` to what the probe reports, or ask the endpoint for a smaller `output_dimensionality` |
| `stale_model` | nothing: the table looks healthy and every distance is meaningless | `build_templates(dataset, rebuild=True)` |
| `stale_dimension` | a stored vector is a different width than the model now returns, so it came from another model | `build_templates(dataset, rebuild=True)` |

`build_templates` refuses on any of these before spending the build; the two
`stale_*` problems do not block `rebuild=True`, which drops the table and so
cannot mix widths or models. Both are also enforced for search:
`has_template_vectors()` returns `False` on a mismatch, so semantic search
falls back to lexical BM25 instead of ranking on noise.

The database reaches its provider through a **server-side secret**, named by
`EMBEDDING_SECRET` (default `gemini`). `bash ./bootstrap.sh` creates it from
`GOOGLE_API_KEY` as a *persistent* secret (and
`uv run python -m backend.db.provision --secret` does just that step), which is
the supported path because it also drops a temporary secret of the same name --
two of them make every call fail as ambiguous. By hand, for Gemini through its
OpenAI-compatible endpoint:

```sql
CREATE SECRET gemini (
    TYPE openai,
    base_url 'https://generativelanguage.googleapis.com/v1beta/openai',
    embeddings_path '/embeddings',
    api_key '<your key>'
);
-- optional: make it the default for ai_embed / ai_similarity
SET sdb_ai_embedding_default_secret = 'gemini';
```

`CREATE SECRET` makes a **temporary** secret that dies with the instance; write
`CREATE PERSISTENT SECRET` (what provisioning does) to have it reloaded at
startup. Every `ai_embed` call in this project names its secret explicitly, so
`sdb_ai_embedding_default_secret` is a convenience for interactive sessions
rather than something the code depends on -- and it is session-scoped anyway.

Native widths differ by model -- `nomic-embed-text:v1.5` is 768,
`gemini-embedding-2` is **3072** (measured through that endpoint) unless the
endpoint is asked for a smaller output -- so `EMBEDDING_DIMENSION` is
configurable and defaults to 3072. The model a table's vectors came from is
recorded at build time (`template_embedding_meta`) because SereneDB cannot
report it, and the stored width is measured from a row (the column reports only
as `ARRAY`), so a change is caught even for a table built before the model was
recorded. `templates_status()` reports `model`, `stored_dimension`,
`model_matches_config` and `dimension_matches_config`; `model: null` means
unattributable rather than proven wrong.

A model change therefore means rebuilding: BGL is 20,352 embedding calls and
OpenStack's current corpus yields 54,043 distinct templates, above the 50,000
`SERENEDB_MAX_TEMPLATES` cap, so it is refused until the cap is raised.

### Team ownership (fictional)

`src/backend/data/team_ownership.csv` is an **invented** mapping from the exact
log components in each corpus to the teams that "own" them. It is not real
organisational data. `count_by_team_tool` loads and joins this table by corpus
and component; unmapped components remain `unowned`, so totals never silently
drop rows. The same CSV is prompt context for naming owners of individual
components.

| Column | Meaning |
| --- | --- |
| `dataset` | Base-corpus dataset (`OpenStack`, `BGL`, or `OTel`) |
| `component` | Exact value of the corpus's `Component`/`ServiceName` field |
| `team` | Fictional owning team |

The measured local components are fully mapped:

| Dataset | Team | Components |
| --- | --- | --- |
| OpenStack | API Gateway | `nova.osapi_compute.wsgi.server`, `nova.api.openstack.wsgi`, `nova.api.openstack.compute.server_external_events`, `nova.metadata.wsgi.server`, `keystonemiddleware.auth_token` |
| OpenStack | Compute Platform | `nova.compute.manager`, `nova.compute.claims`, `nova.compute.resource_tracker`, `oslo_service.periodic_task` |
| OpenStack | Virtualization | `nova.virt.libvirt.driver`, `nova.virt.libvirt.imagecache` |
| OpenStack | Scheduling | `nova.scheduler.host_manager`, `nova.scheduler.filter_scheduler` |
| BGL | BG/L Kernel | `KERNEL`, `APP` |
| BGL | BG/L Control System | `MMCS`, `BGLMASTER`, `CMCS` |
| BGL | Hardware Ops | `MONITOR`, `HARDWARE`, `LINKCARD`, `DISCOVERY` |
| BGL | Network Ops | `SERV_NET` |

The sampled OTel components use these fictional teams: Storefront (`frontend`,
`ad`, `recommendation`, `product-catalog`), Checkout & Payments (`checkout`,
`payment`, `cart`, `currency`, `fraud-detection`, `accounting`), Fulfilment
(`shipping`, `email`, `quote`), Data Platform (`kafka`, `valkey-cart`), and Load
Testing (`load-generator`). OTel is remote and therefore is not accepted by
`count_by_team_tool`; use its bounded service aggregates instead.

`backend.db.ownership` validates the file (header, no empty fields, no component
mapped twice); `bash ./setup.sh` loads it into SereneDB as `team_ownership`
without touching log tables (the same table is reloaded on every `setup.sh`
run). Replace the CSV if the organisers' ownership table becomes available;
keep the three columns and reload it.

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
| `test_chat_client.py` / `test_manage.py` / `test_demo.py` | UI client, provisioning CLI, demo auto-login |
| `test_chat_box_stop.py` / `test_chat_stop_ui.py` / `test_stopped_turns.py` | chat box lock and stop button, stopped/failed runs in the UI, the stored `stopped` flag and the transcript label |
| `test_main_page_live.py` | the live investigation panel: progress lines grouped under the stage markers the backend emits, including a run with none |
| `test_decision_nodes.py` / `test_confidence_prompts.py` / `test_live_confidence.py` / `test_find_evidence.py` | scope & confidence decisions, atomic classifier payloads, gated live confidence checks, evidence-node wiring |
| `test_clarify_and_assumed_scope.py` | what the clarifier is shown, the code-built options line, the assumed-scope line, the no-evidence answer rules |
| `test_normalize_node_direct.py` | the `normalize` generator and a direct `normalize_node` call, whose stage emission is guarded outside a runnable context |
| `test_stream_projection.py` / `test_tool_progress.py` / `test_answer_rendering.py` | streaming projection, tool-progress events, serializer↔renderer sync |
| `test_stream_events.py` | the shared event reducer: backend-saved and live progress agree, refusals never saved as failures, malformed payloads |
| `test_skill_progress.py` | the skill lines in the progress panel: the injected playbook, skills the agent reads (real agent run), failed reads not reported |
| `test_tool_envelope.py` / `test_data_scope.py` / `test_ownership.py` | tool-result contract and query capture, code-built data scope, ownership table |
| `test_evidence_builder.py` / `test_answer_catalog.py` | code-built evidence items and tool calls, the answer prompt's result catalog |
| `test_role_playbooks.py` / `test_answer_ledger.py` / `test_worked_investigation.py` | role objectives and playbooks, findings ledger and priority score, document generator |
| `test_timeline.py` / `test_timeline_graph.py` / `test_claims.py` | the engineer's code-built before/after timeline and its DOT diagram; the number check that downgrades a measured finding whose figures the cited results do not show |
| `test_claim_check.py` / `test_live_claims.py` | the semantic claim check (which claims are sent, the request, the confidence gate, relabel and revision policy, with a fake classifier); gated live cases |
| `test_live_scope_routes.py` | gated live scope-routing over scripted conversations (run with `bash ./routes.sh`) |
| `test_role_switch.py` / `test_role_switch_ui.py` | per-role answer cache, role-switch endpoint and buttons |
| `test_query_layer.py` / `test_budget.py` / `test_search_terms.py` | dataset registry, bounded/metered queries, disk budget, tokenizer |
| `test_cost_guidance.py` / `test_cost_reporting.py` | the cost guidance the agent receives and the per-effort scan budget; cost accounting and its presentation in the answer |
| `test_datasets.py` / `test_trend_compare_queries.py` | registrations (incl. the `openstack` table and selectors) and the per-corpus contract parametrized over `openstack`/`bgl`, trend and period-comparison query shapes |
| `test_auto_complete.py` / `test_session_time_zone.py` | type-ahead merged across `openstack` + `bgl` and the chat-box selector gate; every DB session pinned to UTC |
| `test_registry.py` | local↔MCP tool parity (fourteen tools) |
| `test_tool_specs.py` | every tool is generated from the spec table: same name, description, arguments, bounds and enums on the local tools, the pipeline's MCP wrappers and the server; catalog and kinds derived; no hand-written tool left |
| `test_mcp_contract.py` | the server's contract: instructions, the shared result schema, size limits with the visible cut marker, actionable error messages, the omitted in-process tools |
| `test_mcp_prompts_resources.py` | role prompts built from the pipeline's `evidence_brief`, completions, skill and `logs://` resources rendered from their sources |
| `test_mcp_transport.py` | the bearer token (constant-time), Host/Origin guard, `/health`, stdio round trip; a real-server smoke test gated by `RUN_MCP_SMOKE=1` |
| `test_mcp_operations.py` / `test_mcp_sweep_progress.py` | server operations: lifespan, logging without payloads, rate limiting and the one cached tool; sweep progress to the client and the server's cost cap |
| `test_mcp_bridge_session.py` / `test_mcp_bridge_timeout.py` | the pipeline's MCP bridge: one session kept per bridge, relayed server progress, and the sweep-only call timeout |
| `test_nearby_events.py` | nearby events: schema, the one-statement SQL (both sides, correlation filters, omitted sides), refusals, explanatory notes, the tool on both transports with one shared description |
| `test_otel_index_tool.py` | the in-place OTel index: window coverage, automatic indexing from search, the index tool, no SAVEPOINT or advisory lock |
| `test_catalog.py` | dataset catalog: per-level profile SQL, footer and index-build-window profiles, unloaded/failed datasets, the tool on both transports |
| `test_count_by_dataset.py` | whole-dataset severity counts: one `UNION ALL` statement, local datasets only, unloaded datasets refused, data scope |
| `test_count_by_team.py` | team ownership joins, `unowned` preservation, window/category validation, data scope and measured envelope |
| `test_rank_priorities.py` | the server-only `rank_priorities_tool`: arguments (non-overlapping windows, local-only team), per-dataset scoring with synthetic rows, and the envelope through the in-memory client |
| `test_trace_logs.py` | the server-only `get_trace_logs_tool`: schema, the one-statement SQL (trace id as a parameter, window, microsecond-truncated timestamps), refusals, `t_plus_s` and the explanatory notes, the tool through the in-memory client |
| `test_search_window.py` | time-filtered search: window validation, severity and window inside the ranked branch, the `total_matches` count, the in-place index and semantic branches, the tool on both transports |
| `test_templates.py` / `test_template_build.py` | template extraction, and that the embedding loop provably terminates |
| `test_provision.py` | the setup steps against the database: the secret is persistent and unshadowed, the key never reaches a log, readiness runs a query, and a loaded corpus is not rebuilt |
| `test_embedding_compatibility.py` | the embedding model check: missing secret, width mismatch, vectors from another model, the build refusal and the lexical fallback |
| `test_openstack.py` / `test_openstack_live.py` | OpenStack's request-id derivation, spec and nova-log parser/multi-file loader; gated live checks against the loaded 207,820-row corpus |
| `test_bgl.py` / `test_bgl_live.py` | BGL's parser, epoch handling and long-range bucket behaviour; gated live checks against the full 4.7M-row corpus |
| `test_sampling_and_external.py` / `test_promotion.py` | cluster-sample uncertainty, in-place index plans, promote/digest/teardown |
| `test_sweep*.py` (4 files) | sweep orchestration, effort gating, observability trace, scoring |
| `test_otel_live.py` | gated live checks against the remote corpus and its in-place index |

> The offline suite reports `0 failed, 1339 passed, 107 skipped` with SereneDB
> reachable. Two tests,
> `test_bgl.py::test_an_uppercase_severity_is_refused_for_otel` and
> `test_sampling_and_external.py::test_the_sample_query_tags_every_window`, are
> not gated but need a reachable database, so they fail when `SERENEDB_PORT`
> points at nothing (the host port moves -- see **Limitations**). The skips are
> the gated live cases below. The suite passes identically under
> `TOOLS_TRANSPORT=local` and `TOOLS_TRANSPORT=mcp`.

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

### Live claim checks

`bash ./claims.sh` sends measured claims and the rows they cite to the real claim
check and asserts the code-owned policy: a supported claim is kept, a claim whose
cited rows say nothing about it is relabelled as a hypothesis, and a contradicted
claim sends the answer back for a rewrite. It is gated by `RUN_LIVE_CLAIMS=1`,
must run alone, and writes `.data/claims_report.html` (override
`RUN_LIVE_CLAIMS_REPORT`). The gate (`CLAIM_CONFIDENCE_THRESHOLD`, 0.8) is a
starting value; a judgment below it leaves the finding as written. Cases are
stochastic and must not be re-baselined to match a flipped result.

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
claims.sh     gated live claim-check tests (RUN_LIVE_CLAIMS=1) + HTML report
profile_otel.sh  SereneDB EXPLAIN ANALYZE latency profile for OTel queries
setup.sh      seed SereneDB via loghub (--no-embed by default; optional datasets, --embed)
bootstrap.sh  the whole setup: uv, deps, .env, SereneDB, the embedding secret, corpora
docker-compose.yml  SereneDB with a pinned host port and a named volume (--db docker)
.env.example  annotated environment template, copied to .env by bootstrap.sh
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
│   │   ├── provision.py    server-side setup: wait, secret, memory ceiling, state report
│   │   └── loghub.py       loghub loaders: BGL and OpenStack parsers, raw-line and structured-CSV
│   └── pipeline/
│       ├── graph.py      LangGraph nodes, wiring, and checkpointer selection
│       ├── state.py      pipeline state schema
│       ├── contracts.py  Pydantic agent response models
│       ├── answers.py    structured-answer → marker text serialization
│       ├── roles.py      role objectives and role -> playbook skill map
│       ├── playbooks.py  playbook text and ownership mapping injected into prompts
│       ├── priority.py   deterministic priority score from measured counts
│       ├── timeline.py   engineer's before/after timeline, ordered and timed in code
│       ├── claims.py     checks a measured claim's numbers against the cited results
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
│       └── tools/        specs.py (every tool declared once), local/MCP tool
│                         generators, executor, result envelope and collection
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

## OTel query profiling

`profile_otel.sh` runs SereneDB `EXPLAIN ANALYZE` against the database functions
behind the OTel evidence tools and writes a Markdown report instead of printing
the result table to the terminal. The default report is
`docs/otel_profile.md`; the report includes the selected window, run count,
index coverage, query status, average/median/minimum/maximum milliseconds, and
cumulative total milliseconds. It never builds an uncovered `otel_slice` index.

```bash
bash ./profile_otel.sh
```

All command-line options are:

| Option | Default | Purpose |
| --- | --- | --- |
| `--help` | – | Show the CLI help and exit. |
| `--runs N` | `5` | Run `EXPLAIN ANALYZE` N times per database function. |
| `--start TIMESTAMP` | First OTel footer timestamp | Start of the profiling window; use a UTC timestamp. |
| `--window-seconds N` | `10` | Window length; the remote OTel limit is 300 seconds. |
| `--search TEXT` | `error` | Search text for the existing `otel_slice` search cases. |
| `--only LABELS` | All cases | Comma-separated database-function labels to profile. |
| `--output PATH` | `docs/otel_profile.md` | Markdown report destination; parent directories are created. |

The profiled database-function labels are `count_by_field`, `top_by_field`,
`compare_periods`, `get_field_values`, `get_trend`, `trace_logs`, `search_logs`,
and `count_search_matches`. The last two are the two SQL functions underlying
the search tool; the profiler does not time the tool wrapper itself.

## MCP tool service

A standalone [MCP](https://modelcontextprotocol.io) server exposes the evidence
tools, the investigation skills, ready-made role prompts and the rules the answers
are held to, so an external agent (Claude Code, Claude Desktop, Kiro, MCP
Inspector) can investigate the same logs the chatbot does.

```bash
bash ./mcp_server.sh                                  # Streamable HTTP, default 127.0.0.1:8010/mcp
uv run python -m backend.mcp_server --transport stdio # a process the client launches
TOOLS_TRANSPORT=mcp bash ./backend.sh                 # the chatbot's own pipeline uses the server
```

The backend defaults to `TOOLS_TRANSPORT=local`. Set it to `mcp` to route the
dataset catalog, whole-dataset counts, the eight bounded query tools, the OTel
index tool and the two sweep tools through the server; if the service is unreachable, the backend logs the
failure and falls back to the in-process implementation. `/auto-complete` is not an
MCP tool: it is a chat-box feature and is always answered in-process.

### What the server offers

- **Tools.** The sixteen evidence tools, plus `list_skills` and `read_skill`. Fourteen
  are the pipeline's own. Two are server-only: `rank_priorities_tool` (an external agent
  has no `answer_node`, so it needs the ranking, which the pipeline adds itself) and
  `get_trace_logs_tool` (every log line of one OTel trace id across services, oldest
  first, in a window of at most 300 s; only lines that carry a trace id appear).
  Each is declared once in `src/backend/pipeline/tools/specs.py` and generated for
  the local agent, the pipeline's MCP wrappers and the server, so the name,
  description, arguments and bounds are the same everywhere. The server advertises
  each argument's description, limits, patterns and dataset enum in the tool's JSON
  schema, a title, read-only/idempotent/open-world annotations, and the shared
  result envelope as its output schema. Bad arguments and failures come back as
  messages that say what to fix.
- **Results.** Every result is the envelope described under "Tool results": the
  executed `query`, `filters`, `limit`, `returned`, `total_matches` and `truncated`.
  Text cells over 2,000 characters end in `…[cut]` and a result over 200,000
  characters loses trailing rows (`clipped_cells`, `clipped_rows`, `truncated`
  say so); `get_log_by_line_id_tool` returns the whole row.
- **Instructions.** On connect the server tells the client how to work: start with
  `list_datasets_tool`, follow the role's skill, which results are frequencies, and
  the answer rules -- including that team lead and manager rank with
  `rank_priorities_tool` (and its formula) rather than doing the arithmetic
  themselves. They are rendered from the code that owns each rule. Print them
  with `uv run python -c "from backend.mcp_server.instructions import build_instructions; print(build_instructions())"`.
- **Prompts.** `investigate_as_engineer`, `investigate_as_team_lead`,
  `investigate_as_manager` (arguments `question`, `effort`) and
  `worked_investigation` (`topic`, `effort`): the same briefing the chatbot's
  evidence agent receives (role objective, evidence plan, team ownership, effort
  budget) plus the answer rules and layout.
- **Resources.** The skills (`skill://<name>/SKILL.md`, `skill://<name>/_manifest`),
  `logs://datasets/{dataset_id}/schema`, `logs://ownership` (fictional),
  `logs://roles/{role}`, and `logs://policy/{result-contract,priority-score,effort}`.
- **Health.** `GET /health` (HTTP, no token): 200 when the log database answers,
  503 otherwise, with no detail.

### Configuration

`MCP_HOST`, `MCP_PORT`, `MCP_PATH`, `MCP_URL`, `MCP_CALL_TIMEOUT_S` and
`MCP_SKILLS_ROOT`. Loopback is the default.

| Variable | Default | Purpose |
| --- | --- | --- |
| `MCP_CALL_TIMEOUT_S` | `60` | How long the chatbot's MCP bridge waits for an ordinary tool call |
| `MCP_SWEEP_TIMEOUT_S` | `3600` | How long the bridge waits for a tool the server leaves unbounded: `sweep_chunks_tool` / `investigate_chunk_tool` (a sweep with four promotions takes about three quarters of an hour) and `otel_index_tool` / `search_logs_tool` (an uncovered OTel window builds an index in about 450 s). It only bounds the wait, see below |
| `MCP_MAX_PROMOTIONS` | `4` | Most chunks one call may read message text for (about eleven minutes each). A call above it is refused with the limit and what to retry with; `0` allows only the free passes and refuses `investigate_chunk_tool` |
| `MCP_ENABLE_SWEEP` | `true` | `false` removes the two sweep tools from the server (tag `sweep`) |
| `MCP_RATE_LIMIT_RPS` | `50` | Requests per second one client may make (bursts up to twice that), counted per bearer token or as one anonymous client; `0` turns it off. A tool call is two requests |
| `MCP_CACHE_TTL_S` | `300` | Seconds a `count_by_dataset_tool` result is reused (the only cached tool); `0` turns it off |

**What the sweep timeout does and does not do.** The tools run blocking database
code on a worker thread, and a thread cannot be interrupted: a tool `timeout`
(FastMCP's, or the bridge's wait) does not stop it. The sweep's own wall-clock
budget (`max_seconds`, 900 s) and SereneDB's statement timeout are what stop the
work, and the sweep always drops its index and returns the disk before it
returns, even when the caller has given up. A sweep reports each stage as an MCP
progress notification, which also keeps a quiet connection from looking idle to
a proxy; a proxy that still cuts a long stream is not handled (no event store).
The server logs and times every request without its arguments, and opens and
closes the database pool with itself. Set `MCP_AUTH_TOKEN` (a bearer token,
compared in constant time) before binding `MCP_HOST` to a non-loopback address; the
server refuses an unauthenticated non-loopback configuration. The HTTP transport
validates the `Host` and `Origin` headers (DNS-rebinding protection): `localhost`,
`127.0.0.1` and the bound address are accepted, and further hostnames are added
with FastMCP's `FASTMCP_HTTP_ALLOWED_HOSTS='["mcp.example.com"]'`. The server loads
without any of the chatbot's model keys. FastMCP is pinned in `pyproject.toml`;
regenerate `requirements.txt` with `uv export` after changing dependencies.

### Connecting an external AI agent

HTTP (start the server first):

```text
Endpoint: http://127.0.0.1:8010/mcp
Transport: Streamable HTTP
Authorization: Bearer <MCP_AUTH_TOKEN>      # only when MCP_AUTH_TOKEN is set
```

```json
{
  "name": "ai-chatbot-serenedb",
  "transport": "streamable-http",
  "url": "http://127.0.0.1:8010/mcp",
  "headers": { "Authorization": "Bearer <MCP_AUTH_TOKEN>" }
}
```

stdio (the client launches the server; `fastmcp.json` describes it, so
`fastmcp install claude-desktop fastmcp.json` or `fastmcp install claude-code
fastmcp.json` can register it):

```json
{
  "mcpServers": {
    "serenedb-logs": {
      "command": "uv",
      "args": ["run", "--project", "/path/to/ai-chatbot-test",
               "python", "-m", "backend.mcp_server", "--transport", "stdio"]
    }
  }
}
```

Keep the actual token out of committed files. After connecting, the agent receives
the server's instructions; pick a role prompt (for example `investigate_as_manager`)
to start the same workflow the chatbot runs. The external agent talks to the MCP
service directly, so the backend does not need `TOOLS_TRANSPORT=mcp` for it.

## Limitations

Every entry below is something that bites in the current build, with the
measurement or the exact error behind it. Where a workaround exists it is named.

### Embedding and semantic search

- **A full template build exceeds the embedding provider's quota.** Embedding
  costs **44-53 ms per template** in the batched `UPDATE` the build issues
  (20-23 templates/s), which puts BGL's 20,352 templates at 15-18 minutes, and
  the work is **not** one request per template: `ai_embed` sends up to
  `sdb_ai_embedding_max_batch_size` (default 100) texts per provider request,
  so that build is roughly 200 requests. It still ran out of quota -- a 200-row
  batch failed with `HTTP 429: You exceeded your current quota` -- which says
  the limit is on the texts embedded, not the requests sent. The batch rolls
  back whole (0 of 600 rows written, so nothing is half-embedded) and the window
  recovers on its own: the first call after the limit took 7.96s, then 1.97s,
  then 0.40s and 0.32s.
- **`sdb_ai_throw_on_error = false` does not rescue a quota failure.** The
  setting returns NULL for a row whose request fails, but SereneDB documents
  authentication, not-found, validation and **exhausted-quota 429** errors as
  always failing the query, so the build still dies. What does work is capping
  the spend per statement: with `sdb_ai_max_api_calls_per_query = 2`,
  `sdb_ai_throw_on_quota_exceeded = false` and one text per request, exactly 2
  of 5 rows came back embedded and the other 3 stayed NULL, with no error. Since
  a build fills only NULL embeddings, repeated `build_templates(dataset)` passes
  then resume where the last one stopped. (At the default batch size of 100 the
  same cap embedded all 5 rows, because they rode in a single request -- the cap
  counts requests, not texts.)
- **BGL's stored vectors are the wrong width for the configured model.** Its
  20,352 vectors are 768-wide (built by `nomic-embed-text:v1.5`) while
  `gemini-embedding-2` returns 3072, so `has_template_vectors()` is `False` and
  search falls back to lexical BM25 until they are rebuilt. Nothing recorded
  which model produced them, so the width is measured from a stored row.
- **OpenStack cannot have template embeddings at all.** Its 207,820-row corpus
  yields **54,043 distinct templates**, above the 50,000
  `SERENEDB_MAX_TEMPLATES` cap, so the build is refused. One template per four
  rows also means templating is barely collapsing OpenStack's messages, which is
  most of what makes template-level embedding affordable in the first place.
- **A temporary embedding secret dies with the server, and two of them break
  it.** `ai_embed` resolves its endpoint and key from a server-side secret
  (`EMBEDDING_SECRET`, default `gemini`), and `CREATE SECRET` makes a
  *temporary* one that lives only for that instance -- which is why a recreated
  container failed every call with `secret '<name>' not found`.
  `bash ./bootstrap.sh` creates it `PERSISTENT` instead, measured to survive a
  cold start of the same data directory. The trap is that temporary and
  persistent secrets live in **separate storage backends** (`memory` and
  `local_file` in `sdb_secrets()`), so adding the persistent one while a
  temporary one of the same name exists leaves both, and every call then fails
  with *"Ambiguity detected for secret name"*; provisioning drops the shadowing
  one first. `MODEL_BASE_URL` is read by no query path and cannot substitute for
  any of this.

### The in-place OTel index

- **An uncovered search pays a multi-minute build.** The index covers one window
  at a time, and a search outside it rebuilds: **446.7s and 444.6s** measured
  for a one-second window (87,650 rows, 5.1 MiB). The cost is flat, so a
  narrower window is no cheaper -- there is no way to make an uncovered search
  fast.
- **Only one window is searchable at a time.** A build replaces the previous
  index, so coverage moves rather than accumulating; `otel_index_tool` reports
  what is covered, and `release=true` returns the disk (measured 5.1 MiB,
  423.6 -> 418.6 MiB).
- **Only problem severities are indexed** (`error`, `fatal`, `warn`), so a
  full-text search for info-level lines on `otel_slice` cannot be served even
  inside the covered window.
- **A search with no window and no index is refused**, because the request does
  not say what to index.
- **`search_logs_tool` is advertised read-only although it may build.** The MCP
  `readOnlyHint` is per tool, not per call, and flagging every search -- including
  the millisecond local ones -- would misdescribe the common case. The warning
  lives in the tool's description instead.
- **Builds are serialized in-process only.** SereneDB rejects both `SAVEPOINT`
  and `pg_advisory_xact_lock`, so there is no cross-process lock: the backend
  and a separately run MCP server could build at the same time.

### SereneDB operational constraints

- **`memory_limit` is global and reverts on restart.** A freshly started server
  reports **5.7 GiB** against a 512 MiB budget -- measured directly on a new
  `serened` instance, and the configuration `budget.py` records as the cause of
  three earlier OOM kills. Restarting one that had been lowered put it straight
  back to 5.7 GiB, so the ceiling has to be re-applied every time (step 6 of
  `bootstrap.sh`, and every index build does it itself). Two index builds then died
  mid-`CREATE INDEX` ("server closed the connection unexpectedly"), each time
  taking the instance down and changing its host port; holding the ceiling at
  the budget let the same build finish twice (446.7s, 444.6s), which is strong
  circumstantial evidence rather than a kernel log. Index builds now hold it
  themselves (`external.hold_memory_ceiling`), but **the corpus loaders and
  chunk promotion do not**, so those remain exposed.
- **A Docker container's host port drifts unless it is pinned.** It moved
  7891 -> 7892 -> 7893 in a single session, leaving `SERENEDB_PORT` stale and
  every query refused. `docker-compose.yml` now pins the mapping and
  `bash ./bootstrap.sh` writes the port it used into `.env`, so this bites only
  a container started by hand (`docker ps` shows the current mapping). The
  native `serened` server has no mapping and always listens on 7890.
- **Remote Parquet reads fail transiently.** The same read failed 5/5 with
  `Could not resolve hostname` and then succeeded 5/5 minutes later with no
  change, while inline `read_parquet` worked throughout. Retry before
  investigating the caller.
- **The database cannot describe its own external objects.** An external index is
  invisible to `pg_relation_size` and `information_schema` (so its size is
  recorded at build time), and a `FLOAT[n]` column reports only as `ARRAY` (so a
  vector's width is measured from a row).
- **`SERENEDB_BUILD_THREADS` defaults to 16, the server default**, so the knob
  documented as reducing a build's peak memory currently reduces nothing. It is
  the next dial to turn if builds are still killed, at the cost of build time.

### Coverage and measurement

- **The sweep is not exhaustive and never claims to be**: at Extreme effort it
  covers roughly 7% of one of fifty files in ten minutes. See *What this does not
  do* above; a profiled chunk was counted, not read.
- **Remote aggregates are approximate** (computed over pruned row groups), every
  remote query must carry a window of at most 300 seconds, and the remote tier's
  disk allowance is **0 bytes**, so nothing can be built from it.
- **Cluster samples carry wide uncertainty.** 2,000 rows from a few windows are
  about 8 independent observations, so the output quotes a range (26-99%) rather
  than a midpoint.
- **Counts are log entries, never incidents**, and search results are examples
  that never establish frequency.

### Scope and data

- **The hypothesis-test tools the brief asks for are not built.** T8-T12
  (`get_request_trace_tool`, temporal precedence, counterexample, control group,
  recurrence) are specified in `docs/tool_requests.md` and awaiting their owner,
  so the answer's hypotheses are model-written judgements over measured
  evidence, not deterministic tests. T13 has since landed as
  `rank_priorities_tool`.
- **The team ownership table is invented.** `src/backend/data/team_ownership.csv`
  is a fictional `dataset,component,team` mapping, so every "owning team" in an
  answer is fictional until it is replaced.
- **No corpus records which run a row came from.** The three OpenStack logs share
  one table, so a control-group comparison has to use time windows instead.
- **`request_id` exists only on OpenStack**, so request-level correlation is
  refused elsewhere; and the local corpora have no event id, so recurring
  patterns are counted by template.

### Operations and tests

- **Runs are held in-process.** A single uvicorn worker owns the background
  investigation registry, so a backend restart loses any in-flight run (the
  persisted chat history survives).
- **The offline suite is green** (`0 failed, 1,339 passed, 107 skipped`, identical
  under `TOOLS_TRANSPORT=mcp`); the former inline-marker rendering edge cases are
  fixed and covered by `tests/test_answer_rendering.py`.
- **The live corpus tests assume the full corpora.** With only loghub's 2,000-row
  samples loaded, 13 of `tests/test_openstack_live.py` fail on row-count
  assertions; they pass against the 207,820-row corpus.
- **The gated live suites are stochastic and must be run alone** (routes,
  confidence, claims). A flipped row is a product signal to re-run once and then
  judge, never to re-baseline.
- **No lint, format, typecheck or CI is configured** -- none of ruff, black, mypy
  or pyright is installed, so there is no automated check beyond pytest.

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
