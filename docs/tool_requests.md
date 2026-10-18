# Tool requests (hand-off)

Tools the evidence agent needs to cover the brief (`Making Sense of Logs - The
Right Insights for the Right People.pdf`). T1-T7 and T13 are built and in the
spec table (T13 `rank_priorities_tool` as a server-only entry, `pipeline=False`);
T8-T12 (request trace and the four hypothesis tests) were dropped on 2026-10-18
and will not be built (see "Dropped" below). Nothing is pending. Written against
the current data layer: corpora are
registered in `backend/db/datasets.py` (`openstack`, `bgl`, `otel_s3`,
`otel_slice`), every tool takes a `dataset` id (never a table name), and rows use
the canonical fields (`ts`, `severity`, `service`, `entity_id`, `message`,
`template`, `record_id`, `request_id` where declared). Query builders compose SQL
from the dataset's declared fields and refuse a field the dataset lacks
(`unsupported_field`), as `get_nearby_events_tool` does for `request_id`.
Integration on our side (playbooks, verdicts, answer handling) follows once the
tools exist (see AGENTS.md, "Integrating new tools and tables").

## Rules for every tool

- **No assumptions about the data.** Never hard-code which levels, components,
  tables, windows or patterns exist. Discover them by query.
- **Counts come from SQL**, never from the number of returned rows.
- **Result envelope.** Return `status`, `operation` and `data` as today, plus:
  - `query`: the executed SQL, bound (`cursor.query`);
  - `filters`: the filter arguments that were applied;
  - `limit` and `returned`;
  - `total_matches`: the full count without `LIMIT` when it is cheap, else `null`;
  - `truncated`: `total_matches > returned`, or `null` when unknown.
  The spec table builds this with `pipeline/tools/envelope.py::run_enveloped`
  for every transport; a tool's DB function only returns rows and `QueryStats`.
- **Rows must be JSON-safe.** Convert timestamps to ISO strings and Decimals to numbers.
  Otherwise LangChain serializes the result as a Python repr and the result is dropped.
- **One spec per tool.** A tool is one `ToolSpec` entry in
  `pipeline/tools/specs.py` (plus your DB function and its argument model in
  `tools/schemas.py`); the local tool, the MCP wrapper, the MCP server tool, the
  result kind and the classifiers' catalog line are generated from it. The
  `description` says what the tool is for, when to use it and which tool instead,
  the output columns, the limits, and what an empty result means; it does not
  repeat the inputs, which the parameter schema describes (every parameter needs a
  `Field(description=...)`, every `limit` its bounds). This is what the "Best MCP
  Server" judging reads. See `docs/mcp_server_plan.md`.
- **Limits:** a default and a maximum on every list-returning tool (see each tool).
  Validate time windows (end after start), as `GetTrend` does.
- **Tests:** offline tests of the query built (SQL and parameters), plus at least one
  live check against a seeded SereneDB.

## Dropped (2026-10-18)

T8-T12 are not requested any more. Do not build them, and do not add placeholder
specs for them.

| # | Tool | What covers the need instead |
| --- | --- | --- |
| T8 | `get_request_trace_tool` | `get_nearby_events_tool(same_request=true)` (OpenStack only; at most 50 lines either side of the anchor, plus the exact request-context summary) |
| T9 | `test_temporal_precedence_tool` | nearby events around the hit; any precedence claim stays a labelled hypothesis |
| T10 | `test_counterexample_tool` | the `log-investigation` skill's counterexample query, run with the existing search and count tools |
| T11 | `test_control_group_tool` | `compare_periods_tool` over two measured windows |
| T12 | `test_recurrence_tool` | `get_trend_tool` and the isolated-or-recurring count in the engineer playbook |

Consequences: no tool returns kind `test` (the kind stays in the envelope's
vocabulary but is unused), there is no `pipeline/hypothesis_tests.py`, and
hypothesis `status` stays the evidence agent's judgement over the existing
evidence items rather than a verdict computed from test numbers.

## For the MCP server (see `docs/mcp_server_plan.md`)

| # | Tool | Inputs | Output rows | Notes |
| --- | --- | --- | --- | --- |
| T13 | `rank_priorities_tool` (kind `derived`, offered on the MCP server only -- the pipeline adds its own ranking in `answer_node`) | `datasets`, `group_by` (`template`, `service` or `team`), `baseline_start/end`, `recent_start/end` | the ranked rows, the formula, the terms used and the executed queries, in the standard envelope | Runs the counts and the period comparison through the same DB functions as `count_by_category_tool` / `count_by_team_tool` / `compare_periods_tool` and scores them with `priority.compute_priorities` and `playbooks.team_for`: no second scoring or SQL path. Never accepts numbers from the caller, so an external agent cannot rank by anything but measured counts. |
