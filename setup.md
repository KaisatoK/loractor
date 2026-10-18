# Setup

Everything needed to go from a fresh checkout to a running app. All commands run
from the repository root, and the shell scripts are **not executable** — invoke
them with `bash` (for example `bash ./bootstrap.sh`), never `./bootstrap.sh`.

- `bash ./bootstrap.sh` sets everything up: dependencies, `.env`, SereneDB, the
  embedding secret and the log corpora. It replaces the older manual
  `bash ./setup.sh` flow (bootstrap calls that loader itself as step 7).
- `bash ./backend.sh` and `bash ./frontend.sh` run the app.
- `bash ./mcp_server.sh` runs the optional MCP tool service for external agents.
  The MCP server and the app are independent — see [MCP server](#6-optional-mcp-server).

Deep reference material (why the corpora are queried the way they are, the
embedding model, semantic search, the full HTTP API) lives in
[README.md](README.md).

## 1. Prerequisites

`bash ./bootstrap.sh` installs and checks all of this; the list is what it needs,
and what it will offer to install.

- A Linux host with `curl` and `python3` (the SereneDB installer is Linux-only;
  elsewhere use `bash ./bootstrap.sh --db docker`).
- Python 3.14+ and [uv](https://docs.astral.sh/uv/) — bootstrap installs uv if it
  is missing, and uv provides the Python.
- A SereneDB instance: bootstrap starts one as a native binary (`serened`, no
  root) or in Docker, or uses the one `SERENEDB_*` already points at. It speaks
  the PostgreSQL wire protocol on port 7890 and needs no credentials by default.
- The four provider API keys (see below) — the backend reads them at import and
  will not start without them.
- An embedding provider the **database** can reach — only for the optional
  template embeddings; `bootstrap.sh` loads corpora with `--no-embed` and needs
  no embedding model. `ai_embed` runs inside SereneDB and resolves its endpoint
  and credentials from the server-side secret the calls name
  (`EMBEDDING_SECRET`, default `gemini`), so the host's own reachability is
  irrelevant and `MODEL_BASE_URL` does not change where embeddings come from.
  See **Changing the embedding model** in [README.md](README.md).
- The raw loghub logs in `src/backend/data/` (gitignored): the three OpenStack
  nova runs `openstack_normal1.log`, `openstack_normal2.log`,
  `openstack_abnormal.log`, and `BGL.log` (743 MB; `BGL_LOG_PATH` may point
  elsewhere). These are **optional**: bootstrap falls back to the bundled
  2,000-row loghub samples for whichever corpus is missing, and says so.
- Nothing extra for the remote OTel corpus: it is read from public S3 in place.

## 2. `.env`: copy the template and add your API keys

`.env.example` is the annotated template and `.env` is gitignored. Copy it and
fill in the four provider keys:

```bash
cp .env.example .env
```

```dotenv
GROQ_API_KEY=...
DEEPSEEK_API_KEY=...
GOOGLE_API_KEY=...
TYPESAFE_API_KEY=...

# Optional ports (defaults: 8000 backend, 8501 UI)
BACKEND_PORT=8000
FRONTEND_PORT=8501
```

The keys are loaded automatically on import via `python-dotenv`; all four are
read at import time (the chat clients and the TypeSafe classifier are built
then), so the backend will not start until each has a value. `GOOGLE_API_KEY` is
also the key the database's embedding secret is created from. The UI reads
`BACKEND_PORT` to reach the backend, so both scripts stay in sync. Keep the real
keys out of committed files.

`bash ./bootstrap.sh` does the copy for you if `.env` is missing and tells you
which keys are still unset — its database steps run without the keys, but
`bash ./backend.sh` will not start until they are set.

There is nothing else you must put in `.env` to run the app. The remaining
settings are optional; see [Environment reference](#7-environment-reference).
When the app talks to the MCP server, set the MCP values, in particular
`MCP_CALL_TIMEOUT_S=500` (see [MCP server](#6-optional-mcp-server)).

## 3. Run the setup: `bash ./bootstrap.sh`

```bash
bash ./bootstrap.sh          # everything: uv, deps, env, server, secret, corpora
```

That is the whole setup on a Linux host, and it replaces the older standalone
`bash ./setup.sh` step. It runs eight steps, each of which it skips when already
done, so **re-running it is the way to recover an instance** after a reboot or a
recreated container:

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

Two steps download and execute a vendor installer — uv from `astral.sh` and
SereneDB from `linux.serenedb.com`. Both print the exact command and ask first
unless `--yes` is given; `--db existing` with uv already installed avoids them
entirely.

**Step 4 in each mode.** `--db native` installs `serened` (no root; it listens on
`127.0.0.1:7890` and keeps its data, including persistent secrets, in
`.data/serenedb`), writes its pid to `.data/serened.pid` and logs to
`.data/serened.log`. `--db docker` uses `docker-compose.yml`, which **pins the
host port** to `SERENEDB_PORT` and keeps a named volume, so the port stops
drifting when the container is recreated and the loaded corpora survive; it falls
back to `docker run` when `docker compose` is absent, and if the daemon refuses
the connection it says how to fix it and points at `--db native`.

**What step 6 does, and why it is not optional.** `ai_embed` runs inside SereneDB
and resolves its endpoint and key from a server-side secret, so no host-side
setting can replace it. The secret is created `PERSISTENT`, which is what makes
it survive a restart — verified by stopping a server and cold-starting it on the
same data directory, after which `ai_embed` answered with no re-creation. The
step also lowers the server's global `memory_limit` to the budgeted 512 MiB; a
freshly started instance reports **5.7 GiB**, the configuration that OOM-killed
index builds.

The database-side steps are `backend.db.provision`, which is usable on its own:

```bash
uv run python -m backend.db.provision --wait --memory --secret --report
uv run python -m backend.db.provision --report --probe-embedding   # one provider call
uv run python -m backend.db.provision --json                       # the same state as JSON
```

## 4. Loading the corpora

Bootstrap step 7 loads the ownership table and the corpora through the loghub
loader (`backend.db.loghub`); this is the step that used to be the whole of
`bash ./setup.sh`. With no options it loads these, in this order:

1. `team_ownership` — `src/backend/data/team_ownership.csv`, (re)loaded before
   any corpus. This recreates only the ownership table and does not touch log
   tables.
2. `openstack` — one table from the three nova logs, read one after another in
   chronological order: `src/backend/data/openstack_abnormal.log` (2017-05-14),
   `openstack_normal1.log`, `openstack_normal2.log` (2017-05-16 to 05-17),
   207,820 lines. `LineId` runs on across the files, so it is unique in the table
   and its order is time order; nothing records which run a row came from.
3. `bgl` — `src/backend/data/BGL.log` (or the file named by `BGL_LOG_PATH`),
   4,747,963 lines. This takes a long time.

Each run drops and rebuilds the table, and the disk budget is checked before
anything is built. The script stops at the first failure, including a missing
source file, with the loader's message naming what to add. Timestamps are stored
as `TIMESTAMPTZ` in UTC and every database session is pinned to UTC, so a time
window means the same thing on every machine.

Use the bootstrap flags to change what is loaded:

```bash
bash ./bootstrap.sh --samples            # both bundled 2,000-row samples instead
bash ./bootstrap.sh --datasets openstack # only this corpus (already loaded: add --force-reload)
bash ./bootstrap.sh --skip-data          # server only, no corpora
bash ./bootstrap.sh --embed              # also build template embeddings
```

`--embed` builds **template-level** embeddings and needs the embedding service
from [Prerequisites](#1-prerequisites); it affects templates only and never
embeds the content of each individual log entry. Because it drops and reloads the
corpus table, it is the wrong tool for finishing an incomplete embedding build on
an already-loaded corpus — use `build_templates` directly, as described under
**Semantic search: making sure it runs** in [README.md](README.md).

`setup.sh` still exists as the loader bootstrap invokes; call it directly only
for things bootstrap does not expose, for example the OTel index warm-up:

```bash
bash ./setup.sh --external-index          # ensure the first 5 seconds is searchable
```

`--external-index` is exclusive: it skips ownership and local corpus loading, then
invokes `backend.db.external.ensure_searchable` over the first 5 seconds of the
OTel Parquet data, starting at the first footer timestamp. If the window is not
already covered, `ensure_searchable` builds it in place; if it is covered, it
does no build. The operation stores no log rows locally and can take several
minutes when a build is needed.

## 5. Run the app

With the setup done and the API keys in `.env`, start the two processes from the
repository root (in two terminals):

```bash
bash ./backend.sh     # uvicorn backend.api:app --port $BACKEND_PORT --reload
bash ./frontend.sh    # streamlit run src/ui/streamlit_app.py --server.port $FRONTEND_PORT
```

Then open the UI at `http://localhost:8501` (or `FRONTEND_PORT`). Set
`BACKEND_PORT`/`FRONTEND_PORT` in `.env` (or the environment) to run on
non-default ports; `8000`/`8501` are used when unset.

- The backend provisions the demo login account on start; `bash ./frontend.sh`
  alone does not, so the UI will not boot until that account exists. The demo
  auto-login is on by default; set `AUTH_DEMO_AUTOLOGIN=false` to see the real
  sign-in page. See **Authentication** in [README.md](README.md).
- To see tool calls and their parameters (and, at `DEBUG`, the SQL and query
  params) in the backend terminal:

  ```bash
  LOG_LEVEL=DEBUG bash ./backend.sh
  ```

- `GET /healthz` and `GET /readyz` (on `BACKEND_PORT`) report liveness and
  readiness.

## 6. Optional: MCP server

The standalone [MCP](https://modelcontextprotocol.io) server exposes the same
evidence tools, skills and role prompts to an external agent (Claude Code, Claude
Desktop, Kiro, MCP Inspector). Run it with:

```bash
bash ./mcp_server.sh                                  # Streamable HTTP, default 127.0.0.1:8010/mcp
uv run python -m backend.mcp_server --transport stdio # a process the client launches
```

**The MCP server and the app are independent.**

- **The MCP server is not needed to run the app.** `bash ./backend.sh` and
  `bash ./frontend.sh` work on their own, using the in-process database tools.
- **The app is not needed to connect an external agent to MCP.** An external
  agent talks to `mcp_server.sh` directly; the backend does not have to be
  running for it.
- **If the app itself should use the MCP server** for its evidence tools, set
  `TOOLS_TRANSPORT=mcp` in `.env` (the backend defaults to `local`). You can also
  do it per run:

  ```bash
  TOOLS_TRANSPORT=mcp bash ./backend.sh
  ```

  If the MCP service is unreachable the backend logs the failure and falls back
  to the in-process tools, so the app still answers.

**`MCP_CALL_TIMEOUT_S`.** The chatbot's MCP bridge waits `MCP_CALL_TIMEOUT_S`
seconds (default `60`) for an ordinary tool call. That is shorter than an OTel
search that has to build its in-place index (**~450 s**) or a sweep, so when the
app uses the MCP transport set:

```dotenv
MCP_CALL_TIMEOUT_S=500
```

`.env.example` documents this. The long-running tools use a separate
`MCP_SWEEP_TIMEOUT_S` (`3600`). The rest of the MCP configuration
(`MCP_HOST`, `MCP_PORT`, `MCP_PATH`, `MCP_AUTH_TOKEN`, `MCP_RATE_LIMIT_RPS`,
`MCP_MAX_PROMOTIONS`, …) is documented under **MCP tool service** in
[README.md](README.md), which also has the client configuration for connecting an
external agent over HTTP or stdio. A non-loopback `MCP_HOST` requires
`MCP_AUTH_TOKEN`.

## 7. Environment reference

`.env` is loaded on import. Only the four provider keys are required; everything
else has a default. The backend serves many users at once, so the concurrency
defaults below are sized for roughly a few dozen concurrent investigations.

| Variable | Default | Purpose |
| --- | --- | --- |
| `GROQ_API_KEY` / `DEEPSEEK_API_KEY` / `GOOGLE_API_KEY` / `TYPESAFE_API_KEY` | – | Provider keys; required at import |
| `BACKEND_PORT` / `FRONTEND_PORT` | `8000` / `8501` | Backend and UI ports |
| `MAX_THREADS` | `100` | anyio worker-pool ceiling; bounds in-flight streams |
| `MAX_GLOBAL_INFLIGHT` | `32` | Server-wide investigations; excess requests get `503` |
| `MAX_INFLIGHT_PER_USER` | `2` | Per-user investigations; excess get `429` |
| `SERENEDB_POOL_MIN` / `SERENEDB_POOL_MAX` | `2` / `32` | Shared SereneDB connection pool |
| `SERENEDB_STATEMENT_TIMEOUT_MS` | `30000` | Server-side per-query timeout |
| `SERENEDB_HOST` / `SERENEDB_PORT` / `SERENEDB_DB` / `SERENEDB_USER` / `SERENEDB_PASSWORD` | `localhost` / `7890` / `postgres` / `postgres` / `postgres` | SereneDB target (with Docker the host port is the mapped one, see `docker ps`, and can change when the container is recreated) |
| `TEMPLATE_VECTOR_CACHE_TTL_S` | `60` | Seconds "template vectors available" is cached; a CLI-built embedding table is noticed within this time (`0` re-checks every search) |
| `CHECKPOINT_BACKEND` | `sqlite` | Graph state store: `sqlite`, `memory`, or `postgres` |
| `CHECKPOINT_SQLITE_PATH` | `./.data/checkpoints.sqlite` | SQLite checkpoint file |
| `CHECKPOINT_POSTGRES_URL` | – | Required when `CHECKPOINT_BACKEND=postgres` |
| `MODEL_MAX_RETRIES` / `MODEL_RETRY_BASE_DELAY` / `MODEL_RETRY_MAX_DELAY` | `3` / `1.0` / `20.0` | Retry/backoff for rate-limit and connection errors |
| `APP_DB_PATH` | `./.data/app.sqlite` | Users, sessions, chats, and messages |
| `SESSION_TTL_HOURS` | `168` | Bearer-session lifetime |
| `AUTH_REGISTRATION_OPEN` | `false` | Allow self-service sign-up via `/auth/register` |
| `MCP_CALL_TIMEOUT_S` | `60` | How long the MCP bridge waits for an ordinary tool call; set `500` when the app uses MCP |
| `TOOLS_TRANSPORT` | `local` | `mcp` routes the app's evidence tools through the MCP server |

`GROQ_API_KEY`, `DEEPSEEK_API_KEY`, `GOOGLE_API_KEY` and `TYPESAFE_API_KEY` are
read at import; `.env.example` is the annotated template and `bootstrap.sh`
reports any that are still unset.

## 8. Verify and recover

```bash
uv run pytest                                   # the offline suite
uv run python -m backend.db.provision --report  # SereneDB state, templates, embeddings
```

After a reboot or a recreated container, **re-run `bash ./bootstrap.sh`**: it
restarts the server, re-applies the memory ceiling, re-checks the secret, and
skips the corpora that are already loaded. Two things commonly go stale:

- a recreated Docker container can change its published host port — update
  `SERENEDB_PORT` in `.env` to match `docker ps` (a native `serened` always
  listens on `7890`);
- a restarted server reports its memory limit back at 5.7 GiB, which OOM-kills
  index builds — bootstrap step 6 (and every index build) re-applies the 512 MiB
  ceiling.

If the backend exits at startup, check that all four provider keys are set in
`.env`. If the UI shows no chats, check that the backend is reachable on
`BACKEND_PORT` and that the demo account exists (it is created by
`bash ./backend.sh`).
