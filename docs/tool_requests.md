# Tool requests (hand-off)

Tools the evidence agent needs to cover the brief (`Making Sense of Logs - The
Right Insights for the Right People.pdf`). Written against the data layer of
`b860c25` ("Migrate open stack log"): columns `level`, `component`, `addr`,
`log_timestamp`, `line_id`, `content`; one table per source file; every tool
takes the table explicitly. Integration on our side (prompts, playbooks, result
kinds) follows once the tools exist (see AGENTS.md, "Integrating new tools and tables").

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
  Our branch builds this with `pipeline/tools/envelope.py::run_enveloped`; use it after the merge.
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

## Required by the brief

| # | Tool | Inputs | Output rows | Notes |
| --- | --- | --- | --- | --- |
| T2 | `get_nearby_events_tool` | `table_name`, `line_id`, `before` (default 5, max 50), `after` (default 5, max 50), `same_pid` (bool, default false), `same_request` (bool, default false; same `req-` id as the anchor's `addr`) | ordered by `line_id`: `line_id`, `log_timestamp`, `level`, `component`, `pid`, `addr`, `content`, `offset` (negative before the anchor, 0 for the anchor, positive after) | Brief: "nearby events" for the engineer. An unknown `line_id` returns `status=success` with empty `data` and an explanatory field, not an exception. |
| T3 | `count_by_team_tool` | `table_names` (list), `start_time?`, `end_time?`, `level?` | `team`, `level`, `count`, ordered by count | `JOIN team_ownership ON team_ownership.component = t.component` (and `dataset`). Unmapped components count as team `unowned`, so totals never drop rows. Needs the `team_ownership` table (on our branch it is in `src/backend/data/team_ownership.csv`, loaded by `setup.load_ownership`; move it to `data/`). |

## For hypothesis tests (phase 2, after T2–T3)

Each returns raw numbers only. Our code decides `supported / contradicted /
inconclusive` from explicit thresholds, so the tools must not return a verdict.

| # | Tool | Inputs | Output |
| --- | --- | --- | --- |
| T8 | `get_request_trace_tool` | `table_name`, `request_id` (`req-<uuid>`), `limit` (default 100, max 500) | every line whose `addr` starts with that request id, ordered by time; `total_matches` |
| T9 | `test_temporal_precedence_tool` | `table_name`, `a_query`, `b_query` (search phrases, same matching as `search_logs`), `within_seconds`, window | `b_total`; `b_preceded_by_a` (B rows with at least one A row in the preceding `within_seconds`); `baseline_windows` and `baseline_with_a` (the same check for random anchor times in the window, fixed seed for repeatability) |
| T10 | `test_counterexample_tool` | same as T9 | `b_total`; `b_without_a`; up to 5 example `line_id`s of B without A |
| T11 | `test_control_group_tool` | `query` (phrase) or `event_id`, `case_tables`, `control_tables` (chosen by the agent from the table descriptions; never hard-coded), `level?` | per group: `matches`, `total_rows`, `rate_per_1k` |
| T12 | `test_recurrence_tool` | `table_name`, `query` or `event_id`, window, `bucket` | `buckets_total`, `buckets_with_match`, `longest_gap_seconds`, `distinct_pids`, `distinct_components`, `distinct_requests` |

## For the MCP server (see `docs/mcp_server_plan.md`)

| # | Tool | Inputs | Output rows | Notes |
| --- | --- | --- | --- | --- |
| T13 | `rank_priorities_tool` (kind `derived`, offered on the MCP server only -- the pipeline adds its own ranking in `answer_node`) | `datasets`, `group_by` (`template`, `service` or `team`), `baseline_start/end`, `recent_start/end` | the ranked rows, the formula, the terms used and the executed queries, in the standard envelope | Runs the counts and the period comparison through the same DB functions as `count_by_category_tool` / `count_by_team_tool` / `compare_periods_tool` and scores them with `priority.compute_priorities` and `playbooks.team_for`: no second scoring or SQL path. Never accepts numbers from the caller, so an external agent cannot rank by anything but measured counts. |
