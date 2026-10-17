# MCP server plan

Plan for turning `src/backend/mcp_server` into a server that an external agent
(Claude Code, Claude Desktop, Kiro, MCP Inspector) can connect to and use to
answer the brief's three role questions without our pipeline. Read with
`AGENTS.md` ("Integrating new tools and tables", "MCP tool service"),
`docs/tool_requests.md` and the brief (`Making Sense of Logs - The Right
Insights for the Right People.pdf`).

The brief's **Best MCP Server** award is judged on: clear tool descriptions,
appropriate tool selection by the agent, useful limits on returned results, and
whether Claude can connect and answer questions using our setup instructions.
Every step below is traced to one of those four, or to a minimal requirement
("shows its work", "runs from a clean checkout").

Mark items done here as they land; record decisions and their reasons so the
next agent does not re-litigate them.

## Status (2026-10-17)

Done, committed locally (not pushed):

| Step | Commit | What landed |
| --- | --- | --- |
| FastMCP 2.14.4 -> 4.1.0 | `dac5b06` (pushed) | `mcp` 2.3.0; two test fixes for FastMCP 4 |
| M1 one spec per tool | `5592636` | `pipeline/tools/specs.py`; local tools, pipeline MCP wrappers and server tools generated from it; `TOOL_KINDS` and `TOOL_CATALOG` derived (catalog byte-identical, so the classifiers' input is unchanged); parameter descriptions and registry-backed dataset enums in `schemas.py`; six hand-written tool modules deleted |
| M2 contract and guidance | `dc5ce53` | rendered instructions, `ToolEnvelope` output schema, size limits in `build_envelope` (`…[cut]`, `clipped_cells`, `clipped_rows`), actionable errors with `mask_error_details`, annotations, version |
| M4 transports and auth | `041ec19` | `--transport http\|stdio`, `fastmcp.json`, `TokenVerifier` with `compare_digest`, Host/Origin guard, `/health` |
| M5 T2/T3 from the tool owner | merge of `origin/main` (`52369c0`, `ac4b2f8`) | `get_nearby_events_tool` and `count_by_team_tool` converted from hand-written tools into `ToolSpec` entries (`run`, operation and filters in `specs.py`; their duplicate descriptions in `schemas.py` and the `count_tools`/`context_tools`/`planned_tools` modules dropped); upstream's trend burst summary (`add_burst_summary`) now runs inside the trend spec's `run`; their tests patch `specs.<name>` once for every transport |
| M3 prompts and resources | `b11c9f9` | `evidence_brief` (pipeline prompt byte-identical across 36 role/effort/scope variants), role prompts, `worked_investigation`, completions, `SkillsDirectoryProvider` and `logs://` resources |

Offline suite: `5 failed, 1025 passed, 92 skipped` under both transports (the 5 are
the known failures in AGENTS.md); MCP tests also pass with
`FASTMCP_MCP_CAMELCASE_COMPAT=false`. Checked against a real server: 401 without
the token, 421 for a foreign Host, the pipeline's `MCPBridge` connecting with the
guard on for `MCP_HOST` `127.0.0.1` and `0.0.0.0`, and a stdio round trip through
both `python -m backend.mcp_server --transport stdio` and `fastmcp run fastmcp.json`.

What is left is listed once, under "Remaining tasks" below.

Deviations from the plan as written: server tools are a `Tool` subclass
(`SpecTool`) that takes the argument model's JSON schema directly, instead of
functions with synthesized signatures; dataset enums are annotation metadata
(`WithJsonSchema`), because langchain-core drops `json_schema_extra` when it
rebuilds a tool's schema; `OVERVIEW_ROLES` moved to `roles.py` because `decision.py`
cannot be imported without `TYPESAFE_API_KEY`.

Gaps measured before the work (in-memory client against `server.mcp`):

| Gap | Evidence | Judged under |
| --- | --- | --- |
| Advertised input schemas carry no descriptions, bounds or enums | `search_logs_tool` lists `query_text: string, limit: integer, dataset: string` -- the 1-50 bound and the dataset vocabulary live in `schemas.py` but are only applied inside the function by `_validated` | descriptions, limits, selection |
| Each tool has three descriptions that drift | local docstrings (`tools/*.py`), one-line MCP-wrapper descriptions (`mcp_tools.py`, e.g. "Count log rows by severity."), server docstrings | descriptions |
| No server instructions or prompts | `initialize_result.instructions is None`; `list_prompts() == []` | connect and answer |
| No annotations or output schema on the DB tools | every DB tool: `annotations=None`, empty `outputSchema` | selection, limits |
| No team names or priority ranking for an external agent | ownership and `priority.compute_priorities` are applied only inside the pipeline (`playbooks.ownership_context`, `graph.answer_node`) | role answers (30% / 25% of the main score) |
| Sweeps are silent for minutes | server tools take no `Context`; `sweep()` already accepts `progress=`, `investigate_chunk()` does not | connect and answer |
| Loopback HTTP has no Host/Origin check (DNS rebinding) and the token compare is a plain `!=` | `__main__.py`, FastMCP default `host_origin_protection=False` | security |
| Pipeline over MCP likely times out on sweeps (from code reading, not run) | `MCPBridge` applies `MCP_CALL_TIMEOUT_S=60` to every call and opens a new client per call | pipeline parity |

## Remaining tasks

The single list of open work. Each item points at the step that holds its
details instead of repeating them. Tick an item here when it lands, and update
the Status table.

**Verification we could not run** (needs a reachable SereneDB and the provider keys)

- [ ] `RUN_OPENSTACK_LIVE=1` and `RUN_BGL_LIVE=1` live tests (and `RUN_OTEL_LIVE=1`
      if the remote corpus matters for the demo).
- [ ] `bash ./routes.sh` and `bash ./confidence.sh`. The classifiers' catalog is
      unchanged, so a flip here is unlikely to come from this work; treat one as a
      product signal (AGENTS.md).
- [ ] `bash ./worked_investigation.sh --topic "<a real recurring issue>"`. The
      evidence agent now reads the new tool descriptions and gets the sweep tools
      last, so its tool choices may differ; re-read the SQL appendix by hand.
- [ ] The two "known" offline failures that need a database
      (`test_bgl.py::test_an_uppercase_severity_is_refused_for_otel`,
      `test_sampling_and_external.py::test_the_sample_query_tags_every_window`)
      pass once SereneDB is up.

**Tool selection by a real agent** (D8 dropped the scripted evaluation; a person
with a model does this by hand)

- [ ] Connect a client (Kiro: add `"ai-chatbot-logs": {"url":
      "http://127.0.0.1:8010/mcp"}` to `.kiro/settings/mcp.json`; Claude Code:
      `claude mcp add --transport http ...`; Claude Desktop: stdio) and confirm it
      receives the instructions, 13 tools, 4 prompts and the resources. Whether
      Kiro surfaces prompts and resources is unverified.
- [ ] Ask one question per role without naming a tool and note the first tool
      called: engineer "why did instances fail to spawn?" (catalog, then a
      search), team lead "what problems keep recurring?" (catalog, then counts by
      template/service), manager "what should we fix first?" (counts and trends,
      not a search). Ask the engineer "what's going on?" with no target: it should
      ask for one.
- [ ] Check every number in the answers cites an executed `query`, hypotheses are
      labelled, and no team is invented (T3 is live) and no ranking is invented while T13 is missing.
- [ ] A wrong first tool is fixed in that tool's `description` in
      `pipeline/tools/specs.py`, never by naming tools in a prompt.

**Waiting for the tool owner** (M5)

- [x] T2 `get_nearby_events_tool` and T3 `count_by_team_tool`: landed on
      origin/main and merged into the spec table (`run` supplied,
      `available=True`, catalog prefix dropped, playbooks name them). Still
      to re-run: `routes.sh` and `confidence.sh` (the catalog changed).
- [ ] T13 `rank_priorities_tool` (requested in `docs/tool_requests.md`):
      server-only spec entry; assert it is absent from the pipeline's tools.

**Our MCP work not started** (D6: only if time remains)

- [ ] M6 long-running sweeps: progress and log notifications from the server
      sweep tools, `progress=` on `sweep.investigate_chunk`, `MCP_MAX_PROMOTIONS`
      (D3) and `MCP_ENABLE_SWEEP` (D2).
- [ ] M7 bridge and operations: one persistent client, a separate timeout for the
      sweep pair (today every bridge call gets `MCP_CALL_TIMEOUT_S=60`, so a sweep
      over `TOOLS_TRANSPORT=mcp` likely times out -- inferred from the code, not
      run), forwarding server progress to `tool_progress`, logging/timing/rate
      limiting (D5) and caching (D4) middleware, the DB pool in the server
      lifespan.

**Documentation and release**

- [ ] Write `setup-instruction.md` (D7): how a judge connects over HTTP and
      stdio, the token, `FASTMCP_HTTP_ALLOWED_HOSTS` for a public host name, and
      the SereneDB settings. Link to the server instructions (`build_instructions`)
      instead of repeating the workflow.
- [ ] Push the MCP commits (`5154ab5`..`42e878f`) to `origin/main`.
- [ ] Set `website_url` on the server once the public repository URL is settled.

**Outside the MCP plan, raised in the same discussion**

- [ ] Show the skill an investigation used in the chatbot's progress panel. Today
      nothing reports it: the role playbook is pasted into the `find_evidence`
      prompt, and `read_file` / `read_skill` calls emit no `tool_progress`. Add a
      progress line in `stream_events.progress_line` (once, per AGENTS.md), emit
      it from `find_evidence_node` for the injected playbook, and from a
      `wrap_tool_call` middleware for `read_file` under `skills/` and `read_skill`.
      Open question: show only skills actually read, or the injected playbook too.

## Ownership

| Work | Owner |
| --- | --- |
| Every tool's behaviour: DB functions in `backend/db/`, the SQL, and the new tools T2 (`get_nearby_events_tool`), T3 (`count_by_team_tool`) and `rank_priorities_tool` (spec in "Requested from the tool owner"), with the AGENTS.md checklist for new tools | the tool owner (not us) |
| How tools are declared and exposed (`ToolSpec` table, schema aliases, descriptions, titles, annotations, result contract), the MCP server (instructions, prompts, resources, transports, auth, progress, middleware) and the pipeline's MCP bridge | us (this plan) |

Coordination point: M1 replaces the hand-written tool definitions in
`pipeline/tools/*.py`, which are also the files the tool owner edits. Before
starting M1, agree the `ToolSpec` shape with them and land it first as a small
change; after that they add each tool as a DB function plus one spec entry. If
any of their tools land before M1, M1 converts them like the existing ones.

## Requested from the tool owner

Hand over with `docs/tool_requests.md` (add it there when the plan is accepted;
T2 and T3 are already listed).

- **`rank_priorities_tool`** (kind `derived`, offered on the MCP server only;
  the pipeline already adds its own ranking in `answer_node`). Inputs:
  `datasets`, `group_by` (`template | service | team`), `baseline_start/end`,
  `recent_start/end`. Runs the counts and the period comparison through the same
  DB functions as `count_by_category_tool` / `count_by_team_tool` /
  `compare_periods_tool`, scores them with `priority.compute_priorities` and
  `playbooks.team_for` (no second scoring or SQL path), and returns the ranked
  rows, the formula, the terms used and the executed queries in the standard
  envelope. Never accepts numbers from the caller.

## Decisions taken (and why)

| Decision | Reason |
| --- | --- |
| One server for both consumers: our pipeline (`TOOLS_TRANSPORT=mcp`) and external agents | one tool contract; the brief asks to "expose the same search and summary functions" |
| **Nothing is defined twice.** Every fact has one home (see "Single-source map") and every other place reads it from there or is generated from it; a parity test guards each pair | copies drift -- the three descriptions per tool and the README's hand-copied workflow already have |
| No `investigate` tool that runs the LangGraph pipeline over MCP | it would be a second entry point to the same agent and would hide the tool selection the award judges. The pipeline (our agent, rules enforced in code) and the MCP server (the judge's agent, rules as guidance) share one tool layer and nothing else is duplicated |
| One `ToolSpec` per tool generates the local tool, the pipeline's MCP wrapper, the server tool and the classifiers' `TOOL_CATALOG` entry | today each tool is written three times (`tools/*.py`, `mcp_tools.py`, `server.py`) with its own description, operation string and filter dict, plus a fourth hand-written catalog line in `prompts/builders.py`. Deriving `TOOL_CATALOG` changes classifier input, so `routes.sh` and `confidence.sh` are re-run once (M1) |
| A tool description states purpose, when to use it, output, limits and what an empty result means -- never its inputs | inputs are described once, in the parameter schema (`Field(description=...)`), which the client already shows next to the description; the current "Inputs: ..." paragraphs repeat them |
| Constraints are advertised in the JSON schema, not only checked in the body | an agent can only respect a limit it can see; FastMCP turns `Annotated[..., Field(...)]` into the schema. Cross-field checks (end after start, both-or-neither) stay in the Pydantic models and raise `ToolError` with a fix hint |
| The result envelope (`tools/envelope.py`) is the output schema; a refusal stays a successful call (`status: "refused"`, `reason`, `suggestion`) | refusals are answers the agent must follow, not errors; the envelope already carries `query`, `filters`, `limit`, `returned`, `total_matches`, `truncated` -- exactly the brief's "include the query and its filters, indicate truncation" |
| Size limits are enforced inside the envelope (`clip` in `run_enveloped`), not by FastMCP's `ResponseLimitingMiddleware` | that middleware strips `outputSchema` from every limited tool and can replace structured output with text; clipping in `run_enveloped` also keeps the local and MCP transports identical (README: "passes identically under both transports"), with the explicit `…[cut]` marker AGENTS.md requires |
| The priority ranking is a server-only tool (built by the tool owner) that runs its own SQL and scores with `priority.compute_priorities` | an external agent has no `answer_node`; it must never pass numbers in for the server to rank (counts come from SQL). The pipeline keeps its derived record, so the tool is not offered to it (no double ranking) |
| Cost confirmation for sweeps stays the two-call `confirm_cost=false` -> `true` | works on every protocol era; `ctx.elicit()` raises on modern (2026-07-28) connections, which `fastmcp.Client` negotiates by default |
| Role prompts are built by the same code as the `find_evidence` prompt | no second copy of role objectives, playbooks, ownership or budgets (AGENTS.md: role files are authoritative) |
| Keep `list_skills`/`read_skill` tools alongside skill resources | the pipeline's MCP mode and tool-only clients use them |
| stdio transport alongside HTTP | the dependable path for Claude Desktop / `fastmcp install`; HTTP stays for Claude Code, Kiro, Inspector and our pipeline |
| The pipeline's `clarify` stage stays; the MCP server gets no clarification step of its own | the server never sees the conversation -- an external agent already talks to its user and asks its own questions. What it lacks is our scope *policy*, so the role prompts (M3) and the instructions (M2) render it from the same constants `decide_scope` uses (`decision.OVERVIEW_ROLES`, `builders.QUERYABLE_TARGETS`): ask for a target unless the user named one; a dataset-wide overview is a valid scope for team lead and manager but not for the engineer. Tool-level elicitation is not needed: tools refuse with a suggestion instead |

## Single-source map

| Fact | Its one home | Read by (generated or imported, never copied) |
| --- | --- | --- |
| Tool name, schema, description, kind, title, annotations, catalog line, DB call | `ToolSpec` table (new, `pipeline/tools/specs.py`) | local tools, `mcp_tools.py` wrappers, server tools, `TOOL_KINDS`, `TOOL_CATALOG`, `tools/__init__.__all__`, `registry.LOCAL_DB_TOOLS`, `mcp_tools.MCP_DB_TOOLS` |
| Parameter bounds, patterns, enums, input descriptions | `tools/schemas.py` (`Annotated` aliases and models) | every transport's input schema |
| Result contract | `tools/envelope.py` (`run_enveloped`, `ToolEnvelope`, `clip`) | every transport's output, `logs://policy/result-contract` |
| Role objectives, role -> skill | `pipeline/roles.py` | pipeline prompts, MCP role prompts, `logs://roles/{role}` |
| Evidence plans, answer rules per role | `skills/*/SKILL.md` | `find_evidence` prompt, answer prompt, MCP prompts, skill resources, `read_skill` |
| Shared answer rules (log entries not incidents; examples are not frequencies; label hypotheses; say what is unknown) | `roles.py` `_COMMON` (rename to a public constant) | pipeline answer prompt, MCP role prompts, server instructions |
| Measured vs example kinds | `envelope.MEASURED_KINDS` / `TOOL_KINDS` | answer catalog, server instructions |
| Scope policy | `decision.OVERVIEW_ROLES`, `builders.QUERYABLE_TARGETS` | `decide_scope`, MCP role prompts, server instructions |
| Effort budgets | `pipeline/effort.py` | pipeline, MCP role prompts, `logs://policy/effort` |
| Priority formula and scoring | `pipeline/priority.py` | `answer_node`, `rank_priorities_tool`, `logs://policy/priority-score` |
| Ownership | `data/team_ownership.csv` via `playbooks.ownership_map` | prompts, `count_by_team_tool` table, `logs://ownership` |
| Investigation workflow for external agents | server instructions (`mcp_server/app.py`, built from the rows above) | the client; `setup-instruction.md` and the README link to it and show how to print it, instead of repeating the steps |

## Decisions on the former open questions (2026-10-17: defaults accepted)

| # | Question | Decided | Applies in |
| --- | --- | --- | --- |
| D1 | Rename misleading tools (`get_data_tool` lists a field's values; `top_in_category_by_service_tool` ranks any field)? | keep the names; fix descriptions and titles instead. After M1 a rename is one `ToolSpec` edit, but it still changes classifier state, skills and stored history, and needs `routes.sh` + `confidence.sh` | M1 |
| D2 | Sweep tools visible to external agents by default? | visible, with the cost gate and a server-side promotion cap; `MCP_ENABLE_SWEEP=false` hides them (tag `sweep` + `mcp.disable(tags=...)`) | M6 |
| D3 | Promotion cap: one env var, or per token? | env var `MCP_MAX_PROMOTIONS`, default 4 (Brutal), so today's behaviour is unchanged; a lower value for judges is a `setup-instruction.md` matter | M6 |
| D4 | Cache `list_datasets_tool` / `count_by_dataset_tool`? | cache `count_by_dataset_tool` only (TTL 300 s); `list_datasets_tool` keeps its documented "measured on each call" | M7 |
| D5 | Rate limiting with one shared SereneDB pool | `RateLimitingMiddleware` keyed per bearer token (`get_client_id`), so the pipeline's 32 concurrent runs are not throttled by a judge's client | M7 |
| D6 | Priority under the time left before evaluation (19 Oct) | must: M1, M2, M4. High value: M3. If time remains: M6, M7. The tool owner's tools land independently | all |
| D7 | Connection settings and judge setup | keep the current settings (`SERENEDB_*`, `MCP_HOST`, `MCP_PORT`, `MCP_PATH`, `MCP_AUTH_TOKEN`); no hosting or new connection settings. How judges connect is written later in `setup-instruction.md` | M4 |
| D8 | Model-based tool-selection evaluation | dropped: no model is available. Tool selection is checked by a scripted, model-free client run and by review of the descriptions (see Verification) | — |

## M1 -- One spec per tool

Judged: descriptions, limits, selection.

- New `pipeline/tools/specs.py`: one `ToolSpec` per tool -- `name`, `title`,
  `args_model` (from `schemas.py`), `description`, `kind`, `annotations`,
  `catalog` (the classifiers' line: tier tag plus a short summary), the DB
  function, and the `operation`/`filters` builders (generalising today's
  `search_operation`, `search_filters`, `trend_operation`, `trend_filters`).
- Three generators read the table: local `@tool`s (`execute_database_tool`),
  the pipeline's MCP wrappers (`_call`), and server tools (`run_enveloped`).
  `TOOL_KINDS`, `TOOL_CATALOG`, `tools/__init__.__all__`, `LOCAL_DB_TOOLS` and
  `MCP_DB_TOOLS` are derived from it. Delete the hand-written copies in
  `tools.py`, `list_tools.py`, `count_tools.py`, `trend_tools.py`,
  `catalog_tools.py`, `sweep_tools.py`, `mcp_tools.py`, `server.py` and
  `builders.TOOL_CATALOG`. The planned T2/T3 entries moved into the table marked
  unavailable until the tool owner landed them (done, see M5). Agree the `ToolSpec` shape with the tool owner first
  (see "Ownership").
- Descriptions: purpose, when to use it (and which tool instead), output
  columns, limits, empty-result meaning, frequency or example -- no inputs.
- `tools/schemas.py`: extract the field definitions into `Annotated` aliases
  (`DatasetId`, `LocalDatasetIds`, `SweepDatasetId`, `WindowStart`, `WindowEnd`,
  `OptWindowStart`, `Severity`, `SearchLimit`, `CountLimit`, `Bucket`,
  `CanonicalField`, ...). Dataset aliases carry `json_schema_extra={"enum":
  dataset_ids()}` generated from the registry (no hand-kept `Literal`). The
  existing models are rebuilt from the aliases, so validation is unchanged; the
  server signatures are generated from the model fields.
- Kept as they are: the tool names, the `runtime` injection rule
  (`test_no_tool_has_an_empty_args_schema`) and progress events.
- Tests: every transport's tool is generated from the table (no stray
  definitions); same description, schema and kind on all three; every advertised
  parameter has a description; every numeric limit has `minimum`/`maximum`;
  every `dataset` parameter has the registry enum; `TOOL_CATALOG` keys equal the
  table's names.

Done when the in-memory client shows the same text and bounds as `schemas.py`
and no tool is written out by hand. Re-run `routes.sh` and `confidence.sh`
(`TOOL_CATALOG` text changes) and `worked_investigation.sh` (the evidence agent
reads the new descriptions).

## M2 -- Server structure, contract and guidance

Judged: all four.

- Split `mcp_server/server.py` into `app.py` (instance, instructions, middleware,
  lifespan), `tools.py`, `prompts.py`, `resources.py`, `auth.py`; `server.py`
  re-exports `mcp` (tests import `server.mcp`, `server.read_skill`,
  `server._SKILLS_ROOT`; FastMCP 4 decorators return the plain function).
- `FastMCP("serenedb-logs", instructions=..., version=<project version>,
  website_url=<repo>, on_duplicate="error", mask_error_details=True)`.
- **Instructions** (under ~250 words), rendered from the single-source rows,
  not written as prose that restates them: call `list_datasets_tool` first and
  take windows from it; read the skill for the user's role (from `ROLE_SKILLS`);
  discover exact values with `get_data_tool` before filtering; which kinds are
  frequencies (from `MEASURED_KINDS`); the shared answer rules (from
  `roles._COMMON`); the scope policy (from `OVERVIEW_ROLES`); a refusal carries
  a suggestion -- follow it and retry; cite the `query` behind every number.
- `README.md` "Connecting an external AI agent": drop the hand-written five-step
  workflow and point to the instructions (with a one-liner that prints them),
  so the workflow exists once.
- Every tool: `title`, annotations and `output_schema=ToolEnvelope` come from its
  `ToolSpec`; `timeout` (bounded tools ~45 s, above the 30 s statement timeout;
  sweep tools none).
- `envelope.py`: `ToolEnvelope` Pydantic model (both success and refusal shapes,
  optional `query_stats`, `reason`, `suggestion`); `clip()` applied in
  `run_enveloped` (per-cell cap with `…[cut]`, a total byte budget, a
  `clipped_cells` count).
- Errors: map database failures to `ToolError` with an actionable message
  ("statement timed out after 30 s; narrow the window", "database unavailable");
  everything else is masked.

Tests: contract test over `list_tools()` (title, annotations, output schema,
described parameters); envelope schema validates a real success and a real
refusal; clipping is identical on both transports.

## M3 -- Role prompts and resources

Judged: selection, connect and answer; role-appropriate insight.

- `prompts/builders.py`: extract a pure `evidence_brief(role, effort)` from
  `PipelinePrompts.find_evidence` (role context, playbook, ownership, effort
  budget, `COST_DISCIPLINE`). The pipeline prompt must stay byte-identical
  (snapshot test).
- Prompts: `investigate_as_engineer(question, effort="Medium")`,
  `investigate_as_team_lead(...)`, `investigate_as_manager(...)`, and
  `worked_investigation(topic)` (all three roles in turn; the brief's worked
  investigation deliverable). Each is assembled from existing sources only:
  `evidence_brief(role, effort)`; the role's answer rules
  (`playbooks.role_answer_rules`, from the skill); the shared answer rules
  (`roles._COMMON`); the answer layout, rendered from the section headings and
  finding labels `answers.py` already uses for the pipeline (export them as
  constants -- Measured / Hypotheses / Limitations, recommended next steps with
  owner, data scope and executed queries -- rather than describing them again);
  the scope policy (from `OVERVIEW_ROLES` / `QUERYABLE_TARGETS`); and the user's
  question. A test fails if any of these strings is written into
  `mcp_server/` directly.
- `@mcp.completion`: `effort`, `role`, `dataset_id`.
- Resources:
  - skills via `SkillsDirectoryProvider(roots=SKILLS_ROOT)`: URIs become
    `skill://<name>/SKILL.md` plus `skill://<name>/_manifest` (path screening
    built in). Replaces the hand-written `skill://{name}`; update `test_mcp.py`
    and the README.
  - `logs://datasets/{dataset_id}/schema` (from the `Dataset` registry),
    `logs://ownership` (from `ownership_map`, labelled fictional),
    `logs://roles/{role}` (from `ROLE_OBJECTIVES` / `ROLE_SKILLS`),
    `logs://policy/result-contract` (from `ToolEnvelope`),
    `logs://policy/priority-score` (from `priority.FORMULA` / `WEIGHTS`),
    `logs://policy/effort` (from `EFFORT_SPECS`). Every resource is rendered
    from its source at read time; none holds text of its own.

Tests: every prompt renders for every role and effort; resources list and read;
the snapshot test above.

## M4 -- Transports and auth (current settings kept)

Judged: connect and answer.

- `__main__.py`: `--transport http|stdio` (HTTP stays the default; stdio logs to
  stderr only; a test asserts the server imports without provider keys and
  writes nothing to stdout).
- Replace `BearerTokenMiddleware` with a `TokenVerifier` subclass
  (`verify_token` compares with `hmac.compare_digest`, returns `AccessToken`),
  passed as `auth=`. Same `MCP_AUTH_TOKEN`, same loopback rule.
- `http_app(..., host_origin_protection=True, allowed_hosts=[...])`, with the
  allowed hosts derived from the existing `MCP_HOST` (plus `localhost` /
  `127.0.0.1` when it is loopback) -- no new setting. Verify whether entries
  need the port before relying on it (the bridge's own requests must pass).
- `@mcp.custom_route("/health")`: unauthenticated, returns ok/503 without
  details (custom routes bypass auth by design).
- `fastmcp.json` at the repo root so `fastmcp install claude-desktop` /
  `claude-code` can register the stdio server.
- Not in this step: how judges connect. That goes into `setup-instruction.md`,
  written later (D7), which links to the server instructions rather than
  repeating them.

Tests: gated HTTP smoke test (401 without token, rejected foreign Host,
`/health`), stdio round trip.

## M5 -- Expose the tool owner's new tools

Judged: role-appropriate insight, search + analytics. The tools themselves are
the tool owner's (see "Ownership"); this step is only what the MCP side checks
when each one lands.

- With M1 in place, a new tool is a DB function plus a `ToolSpec` entry; every
  transport, `TOOL_KINDS`, `TOOL_CATALOG`, `__all__` and the registries follow.
  `rank_priorities_tool` is a spec flagged server-only and is asserted absent
  from the pipeline's tool set.
- Run the M1-M3 contract tests on the new entries (title, annotations, output
  schema, described and bounded parameters) and check that the role prompts and
  instructions pick them up from the sources (for example the
  "NOT AVAILABLE YET" catalog prefix is gone, the playbooks name the tool).
- The AGENTS.md checklist for new tools (skills, `test_registry.py` count,
  `routes.sh`, `confidence.sh`) is part of landing the tool, by whoever lands
  it.

## M6 -- Long-running sweeps

Judged: connect and answer; Working with Huge Datasets.

- Server sweep tools become `async def`, run the blocking work with
  `anyio.to_thread.run_sync`, and forward the existing progress events through
  `anyio.from_thread.run(ctx.report_progress, ...)` and `ctx.info` (`map`,
  `profile`, `sample`, `decide`, `promote`, `done`).
- `sweep.investigate_chunk` gains a `progress=` parameter.
- Server-side cap `MCP_MAX_PROMOTIONS` (D3); `MCP_ENABLE_SWEEP` (D2).
- Optional: `http_app(event_store=EventStore(), retry_interval=...)` plus
  `ctx.close_sse_stream()` between passes, so proxies do not cut a 10+ minute
  stream.

Tests: progress notifications arrive in order through an in-memory client;
the cap refuses above the limit with a suggestion.

## M7 -- Pipeline bridge and operations

Judged: pipeline parity, robustness.

- `MCPBridge`: one persistent client on its loop thread; per-tool timeouts
  (`MCP_SWEEP_TIMEOUT_S` for the sweep pair); a `progress_handler` that forwards
  server progress into `tool_progress` (`mcp_tools.py` sweep wrappers use
  `execute_database_tool(..., with_reporter=True)`), so the UI shows sweep
  stages under `TOOLS_TRANSPORT=mcp` too.
- Middleware: `StructuredLoggingMiddleware` (no payloads), `TimingMiddleware`,
  `RateLimitingMiddleware` (D5), `ResponseCachingMiddleware` for
  `count_by_dataset_tool` only (D4).
- Lifespan: open and close the SereneDB pool with the server.

Tests: bridge reuses one client; a long call outlives `MCP_CALL_TIMEOUT_S` only
for sweep tools; the whole offline suite under `TOOLS_TRANSPORT=mcp`.

## Order and dependencies

```
agree ToolSpec shape with the tool owner
  └─▶ M1 ─▶ M2 ─▶ M3        M1, M2 must; M3 high value
            └──▶ M4         must
            └──▶ M6 ─▶ M7   if time remains
M5 whenever a tool from the tool owner lands (after M1)
```

## Housekeeping

- Done: `README.md` and `AGENTS.md` carry the new offline counts and the
  one-spec-per-tool checklist; `rank_priorities_tool` is requested in
  `docs/tool_requests.md` (T13).
- Still to do: see "Remaining tasks".

## Verification (each step, before marking it done)

- `uv run pytest` (known failures only) and the same under `TOOLS_TRANSPORT=mcp`.
- In-memory client: `list_tools`, `list_prompts`, `list_resources` match the
  contract tests.
- After M1 and M3: `RUN_OPENSTACK_LIVE=1`, `bash ./routes.sh`,
  `bash ./confidence.sh`, `bash ./worked_investigation.sh --topic "<a real
  recurring issue>"` (these use the pipeline's own provider keys); re-read the
  SQL appendix by hand against direct queries.
- After M4, model-free (D8): a scripted `fastmcp.Client` run over HTTP and over
  stdio that, for each role, reads the instructions and the role prompt and
  calls the tools that role's playbook names, asserting each returns the
  envelope with `query`, `filters` and `truncated`; plus a manual pass in MCP
  Inspector. Review every tool description against the brief's three role
  questions: for each question, exactly one tool should read as the right first
  call.

## Risks and unverified points

- Tool selection by a real agent is not tested (D8): the scripted run proves the
  tools work and the descriptions are reviewed, but whether Claude picks the
  right tool first is only known once someone connects it.
- M1 rewrites how every tool is defined, in files the tool owner also edits.
  Mitigation: agree the `ToolSpec` shape first; generate first and compare
  against the hand-written tools (names, schemas, descriptions, kinds, envelope
  output) in a test, then delete the hand-written ones in the same change.
- Deriving `TOOL_CATALOG` from the specs changes what both classifiers read, so
  scope routes and confidence verdicts can move; treat a flip as a product
  signal (AGENTS.md), not a reason to keep a second catalog.
- Richer descriptions and enums change what the pipeline's evidence agent sees
  (same `args_schema`), so routes and worked investigations can move.
- Resolved: FastMCP 4.1 strips the port from both the Host header and each
  `allowed_hosts` entry, so entries are host names only; checked against a real
  server with `MCP_HOST` `127.0.0.1` and `0.0.0.0`.
- Claude Desktop remote connectors are believed to be brokered from Anthropic's
  cloud and unable to reach `localhost` (not verified); `setup-instruction.md`
  should therefore document stdio for Desktop.
- `SkillsDirectoryProvider` changes resource URIs; any external configuration
  that names `skill://<name>` breaks (README, `test_mcp.py`).
- `mask_error_details=True` hides useful messages unless every expected failure
  is raised as `ToolError`; M2's error mapping must cover the query layer's
  exceptions first.
- Sync worker threads cannot be interrupted by a tool `timeout`; the SereneDB
  statement timeout remains the real bound for bounded tools.
