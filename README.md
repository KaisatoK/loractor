# loractor - A Log "Reactor"

**Turn overwhelming production logs into evidence-backed decisions.**

When something fails in production, people need different answers from the same logs:

- **Engineers** need to know what failed, where it failed, and what happened immediately before and after it.
- **Team leads** need to know which problems recur, how often they happen, and which components or teams are involved.
- **Managers** need to know what deserves attention first and what action should happen next.

A generic chatbot gives everyone the same explanation. A dashboard gives you charts without an investigation. Searching every message in a large log corpus is often too expensive.

Loractor provides one workflow for all three:

> **question → evidence → recommendation**

It combines role-specific investigation playbooks with bounded database queries, code-derived metrics, and transparent evidence reporting. The model explains the results, but the database and application code own the measurements.

## Why Loractor

Loractor does not treat every log question as a full-text search.

1. It discovers the available datasets and their time ranges.
2. It uses inexpensive aggregates to find relevant severities, services, trends, and windows.
3. It searches message text only after narrowing the investigation.
4. It computes timelines and priority rankings in code.
5. It shows what the data supports, what remains a hypothesis, and what was not examined.

The result is not just an answer. It is an investigation that can be checked.

## One investigation, three roles

| Role | Question | Evidence | Result |
| --- | --- | --- | --- |
| **Engineer** | What failed, where, and what happened around it? | Matching log lines, nearby events, local spread, and isolated-versus-recurring counts | A timeline, possible cause, and next diagnostic check |
| **Team lead** | Which problems recur and who is involved? | Counts by template, component, team, and time bucket | Patterns to investigate first and why |
| **Manager** | What deserves attention first? | Frequency, severity share, trend direction, and affected breadth | A ranked action list with owning teams |

These are different investigations, not just different answer lengths. Each role has its own evidence plan and answer requirements.

## Benchmark snapshot

The current implementation is designed to investigate large corpora under a bounded local resource budget.
| Capability | Documented result |
| --- | ---: |
| Remote OpenTelemetry corpus | **1 billion rows** queried in place |
| Narrow remote query | Around 5-second window skipped **more than 99%** of row groups |
| In-place text index | About **465 seconds** for roughly 1.4 million rows, without copying source rows |
| Local BGL corpus | **4.7 million rows** with exact counts |

For broad questions, Loractor uses a graded sweep:

```text
map       → profile       → sample       → promote
metadata    cheap counts    limited text    selected full-text read
```

This makes the trade-off explicit:

- A profiled chunk was counted, not read.
- A sampled chunk is evidence with uncertainty.
- A promoted chunk was examined in detail.
- The answer reports which level of coverage was actually achieved.

See the [worked investigation](docs/worked_investigation.md) for a complete captured run across all three roles.

## Grounded answers

Every evidence tool returns a shared result envelope containing the dataset, filters, executed query, row counts, truncation state, and query statistics.

Loractor distinguishes between:

- **Measured findings** — supported by cited aggregate or trend results.
- **Hypotheses** — plausible explanations that the logs do not confirm.
- **Limitations** — what the investigation could not establish.
- **Recommendations** — concrete next steps with a reason and, where available, an owning team.

Important rules are enforced in code:

- Counts are **log entries**, not automatically separate incidents.
- Search hits and nearby events are examples; they do not establish frequency.
- Remote OTel aggregates are labelled approximate when the query cannot be exact.
- A sweep is not exhaustive and never claims that an issue is absent from unexamined chunks.
- The manager priority score is computed from measured results, not invented by the model:

```text
0.4 × volume share
+ 0.3 × error share
+ 0.2 × trend direction
+ 0.1 × breadth
```

Read more in [Grounded answers](docs/grounding.md) and [Known limitations](docs/limitations.md).

## How it works

```text
Streamlit UI
    │
    ▼
FastAPI backend
    │
    ▼
LangGraph investigation pipeline: normalize → scope → clarify/find evidence → answer → confidence
                                                                    │
                                                                    ▼
                                                         role-specific playbook
                                                                    │
                                                                    ▼
                                                            SereneDB query tools
```

The evidence agent uses the same tool layer across the in-process pipeline and the optional MCP server. Dataset capabilities are declared in a registry, so unsupported operations are refused with an actionable explanation instead of silently querying the wrong shape of data.

The main pipeline is:

1. **Normalize** the conversation into a usable question.
2. **Scope** the request and ask for clarification when necessary.
3. **Find evidence** using the selected role's playbook.
4. **Answer** with cited results, timelines, rankings, and recommendations.
5. **Check confidence** and revise the evidence or answer when the result is incomplete or unsupported.

See the [architecture guide](docs/architecture.md) for the detailed graph and component map.

## Datasets

Loractor currently works with four dataset views:

| Dataset | Description |
| --- | --- |
| `openstack` | 207,820 local OpenStack Nova log rows with exact counts and nearby-event lookup |
| `bgl` | 4.7 million local Blue Gene/L RAS log rows spanning seven months |
| `otel_s3` | OpenTelemetry Parquet data queried directly from public object storage |
| `otel_slice` | An in-place searchable index over a bounded OTel window; source rows are not copied locally |

The repository includes small samples. The full OpenStack and BGL corpora are loaded separately during setup.

The included `team_ownership.csv` mapping is fictional and exists for demonstrating team-level recommendations. Replace it with real ownership data in a production deployment.

## Quick start

Requirements:

- Linux, or Docker for SereneDB
- Python 3.14+
- [uv](https://docs.astral.sh/uv/)
- Provider keys configured in `.env`

The bootstrap script performs the complete setup:

```bash
bash ./bootstrap.sh
```

Then start the backend and UI in separate terminals:

```bash
bash ./backend.sh
bash ./frontend.sh
```

Open `http://localhost:8501`, choose a role, and ask a question about the logs.

The offline test suite requires no external services:

```bash
uv run pytest
```

For the full setup guide, environment reference, database options, and recovery instructions, see [setup.md](setup.md).

## Optional MCP server

Loractor can expose the same evidence tools, role playbooks, and dataset resources to external agents such as Claude:

```bash
bash ./mcp_server.sh
```

The MCP server is independent from the Streamlit application. It can be used directly by an external agent, or the application can route its own evidence calls through MCP:

```bash
TOOLS_TRANSPORT=mcp bash ./backend.sh
```

See the [MCP server guide](docs/mcp-server.md) for transport, authentication, and client configuration.

## Testing

Most tests run offline:

```bash
uv run pytest
```

Live corpus tests are gated because they use a database, real corpora, public S3, or model calls:

```bash
RUN_OPENSTACK_LIVE=1 uv run pytest tests/test_openstack_live.py
RUN_BGL_LIVE=1 uv run pytest tests/test_bgl_live.py
RUN_OTEL_LIVE=1 uv run pytest tests/test_otel_live.py
```

Additional live policy checks are available through:

```bash
bash ./routes.sh
bash ./confidence.sh
bash ./claims.sh
```

## Project layout

```text
src/backend/           FastAPI API, jobs, authentication, database, and pipeline
src/backend/pipeline/  LangGraph workflow, evidence tools, answer rendering, and ranking
src/backend/db/        Dataset registry, query builders, loaders, budgets, and OTel indexing
src/backend/appdb/     Users, sessions, chats, and persisted messages
src/backend/mcp_server/ Standalone MCP tool service
src/ui/                Streamlit application, chat UI, progress, and role switching
skills/                Engineer, team-lead, manager, and shared investigation playbooks
docs/                  Architecture, setup, grounding, limitations, and worked investigation
tests/                 Offline tests plus gated live corpus and policy tests
```

## Documentation

- [Quick start](docs/quick-start.md) — shortest path to a first answer
- [Setup guide](setup.md) — full installation and environment reference
- [Architecture](docs/architecture.md) — runtime components and investigation graph
- [Grounding](docs/grounding.md) — findings ledger, citations, and claim checks
- [Known limitations](docs/limitations.md) — current boundaries and operational caveats
- [Tool catalogue](TOOLS.md) — tool arguments, result kinds, and contracts
- [Worked investigation](docs/worked_investigation.md) — an end-to-end captured example

## Status and limitations

Loractor is an active prototype. In particular:

- OTel remote aggregates may be approximate and must be interpreted with their stated scope.
- Broad sweeps are deliberately bounded and are not exhaustive.
- Building an OTel in-place index takes minutes and covers one window at a time.
- The fictional ownership table does not represent a real organization.
- Live route, confidence, and claim checks are gated and may be stochastic.
- In-process background runs are lost if the backend restarts, although persisted chat history remains.

For the authoritative, measurement-backed list, see [Known limitations](docs/limitations.md).

## License

MIT
