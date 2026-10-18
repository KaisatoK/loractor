# Tool requests (hand-off)

Tools the evidence agent needs to cover the brief (`Making Sense of Logs - The
Right Insights for the Right People.pdf`). What remains below are the hypothesis
tests T8-T12. Written against the current data layer: corpora are
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

## For hypothesis tests (phase 2)

Each returns raw numbers only. Our code decides `supported / contradicted /
inconclusive` from explicit thresholds, so the tools must not return a verdict.

Common rules for T8-T12:

- **Datasets:** the local corpora only (`openstack`, `bgl`; `datasets.local_datasets()`).
  Refuse the remote tiers with a suggestion, as `count_by_team_tool` does.
- **Window:** `start_time`/`end_time` (`'YYYY-MM-DD HH:MM:SS'`, UTC, end
  exclusive), required for T9-T12 so every number has a stated scope; validate
  end after start.
- **Matching an event:** each side is a `match` object, exactly one of
  `{"query_text": ...}` (the same BM25 matching as `search_logs_tool`) or
  `{"template": ...}` (an exact `template` value, as found with `get_data_tool`),
  optionally with `category` (severity, checked against the dataset's
  `severity_values` like the other tools). One shared argument model in
  `tools/schemas.py`.
- **Kind** `test` (T9-T12) or `context` (T8). Numbers come from SQL over every
  matching row, never from returned rows; example ids are capped and say so.
- **No verdict** in the output; `pipeline/hypothesis_tests.py` decides.

| # | Tool | Inputs | Output |
| --- | --- | --- | --- |
| T8 | `get_request_trace_tool` (kind `context`) | `dataset` (only datasets that declare `request_id`; today `openstack`, others refused with `unsupported_field`), `request_id` (`req-<uuid>`), `limit` (default 100, max 500) | every row with that `request_id`, ordered by `record_id`: the canonical fields plus `seconds_from_first`; `total_matches`, first/last `ts`, distinct `service` count. Differs from `get_nearby_events_tool(same_request=true)`, which returns at most 50 lines either side of one anchor. |
| T9 | `test_temporal_precedence_tool` | `dataset`, `a` (match), `b` (match), `within_seconds` (1-3600), window | `b_total`; `b_preceded_by_a` (B rows with at least one A row in the preceding `within_seconds`, same dataset); `baseline_windows` and `baseline_with_a` (the same check at random anchor times in the window, fixed seed so a rerun gives the same numbers); `example_record_ids` (up to 5 B rows preceded by A) |
| T10 | `test_counterexample_tool` | same as T9 | `b_total`; `b_without_a`; up to 5 `record_id`s of B rows with no A in the preceding `within_seconds` |
| T11 | `test_control_group_tool` | `dataset`, `match`, `case_start`/`case_end` and `control_start`/`control_end` (two windows chosen by the agent from measured data -- trend, period comparison -- never hard-coded; they may not overlap) | per group (`case`, `control`): `matches`, `total_rows`, `rate_per_1k`. Groups are time windows because a corpus is one table (the three OpenStack runs share one; nothing records which run a row came from). |
| T12 | `test_recurrence_tool` | `dataset`, `match`, window, `bucket` (as `get_trend_tool`; omitted = chosen from the window) | `buckets_total`, `buckets_with_match`, `longest_gap_seconds`, `distinct_entities` (`entity_id`: process on OpenStack, node on BGL), `distinct_services`, `distinct_requests` (`null` where the dataset has no `request_id`) |
