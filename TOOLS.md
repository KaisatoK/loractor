# Tool catalogue

Every evidence tool is declared **once** in `src/backend/pipeline/tools/specs.py` and generated from that table across transports: the pipeline's own agent (in-process, the default) and the standalone MCP server. The name, description, arguments and bounds are therefore identical everywhere. The agent's argument schemas live in `src/backend/pipeline/tools/schemas.py`; the result contract in `src/backend/pipeline/tools/envelope.py`.

## The result envelope

Every tool returns one envelope:

```
status, operation, kind, dataset, query, filters, limit, returned,
total_matches, truncated, data, query_stats
```

plus, for a search, `total_matches_any` and `search_mode`; and, when a cell or row was cut for size, `clipped_cells` / `clipped_rows` (a shortened cell ends in `…[cut]`).

- `query` is the executed SQL with parameters bound — cite it for every number. `filters` records the applied window, severity and field.
- `limit` is the row cap the tool applied, `returned` the rows in `data`, `total_matches` the full match count when the tool counted it.
- `truncated` is `True` when more matching rows exist than were returned, `False` when the result is provably complete, and `null` when neither is known. For a search it follows the ranked pool (`total_matches_any`).
- `query_stats` records what the query cost: rows scanned, elapsed time, files read, whether the counts are approximate, and notes.

A query the layer **refuses** (unbounded, too wide, unsupported on that corpus) is a result, not an error: `status: "refused"`, with a `code`, a `reason` and a `suggestion` naming a request that would be accepted. Follow the suggestion and retry.

## Kinds

`kind` says what the rows are:

- **`aggregate`, `trend`** — measured counts. `aggregate` is a grouped count or rank over the stated scope; `trend` is one value's count per time bucket. These are the only kinds that measure frequency directly.
- **`derived`** — computed in code from measured counts whose queries are listed (the priority ranking, and the engineer's timeline). The timeline is not a frequency and says so.
- **`search`, `context`, `lookup`, `catalog`** — examples or metadata. A **search example never establishes frequency**; count with an aggregate.

Counts are log entries, never "incidents".

## The tools at a glance

| Tool | Kind | Availability | Purpose |
| --- | --- | --- | --- |
| `list_datasets_tool` | catalog | pipeline + MCP | Every dataset's rows, span, levels and a ready time window. |
| `count_by_dataset_tool` | aggregate | pipeline + MCP | Whole-local-dataset severity counts in one query. |
| `get_trend_tool` | trend | pipeline + MCP | One known value's counts per time bucket. |
| `count_by_category_tool` | aggregate | pipeline + MCP | Log rows per severity over a window, optionally grouped. |
| `sweep_chunks_tool` | search | pipeline + MCP | A graded pass over the whole remote corpus (last resort). |
| `investigate_chunk_tool` | search | pipeline + MCP | Read one named chunk's message text. |
| `search_logs_tool` | search | pipeline + MCP | Full-text search over an indexed corpus. |
| `otel_index_tool` | catalog | pipeline + MCP | Report, build or release the in-place OTel index. |
| `top_in_category_by_service_tool` | aggregate | pipeline + MCP | Rank a field's values by log volume. |
| `compare_periods_tool` | aggregate | pipeline + MCP | Compare per-value counts between two windows. |
| `get_data_tool` | catalog | pipeline + MCP | List a field's most common values. |
| `get_log_by_line_id_tool` | lookup | pipeline + MCP | Fetch one complete row by primary key. |
| `get_nearby_events_tool` | context | pipeline + MCP | Lines just before and after one anchor line. |
| `count_by_team_tool` | aggregate | pipeline + MCP | Counts per owning team and severity. |
| `rank_priorities_tool` | derived | **MCP only** | Rank problems per dataset from measured counts. |
| `get_trace_logs_tool` | context | **MCP only** | Follow one OTel trace across services. |

`rank_priorities_tool` and `get_trace_logs_tool` are **MCP-only**: the pipeline's `answer_node` builds its own ranking record, and its graph does not ask for a trace; an external agent has neither, so the server offers both.

## Tool reference

### `list_datasets_tool`

Purpose and when: list every registered dataset with what it holds, measured on this call. Call it first when no time window was named, so windows and severity filters come from the data instead of a guess.

Arguments: none.

Returns: `kind=catalog`, one row per dataset — `dataset`, `kind`, `available` (false = not loaded or not built; `note` says why), `rows`, `rows_exact`, `first_timestamp`, `last_timestamp`, `span_seconds`, `max_window_seconds`, `whole_span_fits_one_window`, `window_start`, `window_end` (a window covering every row, end exclusive), `levels` (`[{severity, count}]`), `timestamp_source`, `note`.

Limits: none. Local datasets are counted exactly; the remote corpus reads file footers only (levels declared, uncounted); the in-place index reports the window it was built over.

Empty/refused: a dataset that is not loaded appears with `available: false` and a `note`, rather than being left out. Catalog rows describe the data; they are not a frequency over an investigated scope.

### `count_by_dataset_tool`

Purpose and when: count log entries per severity in each **whole local dataset**, in one query. Use it for a corpus-wide overview or to compare datasets. For a time window or a breakdown by service, or for the remote corpus, use `count_by_category_tool` instead.

Arguments:

| Name | Type | Constraints | Required | Description |
| --- | --- | --- | --- | --- |
| `datasets` | list of local dataset ids | min 1; local, exact-count datasets only | optional | Datasets to count whole; omit to count every local dataset. |

No time window: every row is counted.

Returns: `kind=aggregate` rows `{dataset, severity, count}`, with no row cap.

Limits: local datasets only.

Empty/refused: an unloaded dataset is refused, never left out of the totals. The remote corpus cannot be counted whole and is rejected — count a remote dataset with `count_by_category_tool` and a window.

### `get_trend_tool`

Purpose and when: count one known field value per time bucket, to see how it moves over a window. Discover the exact value with `get_data_tool` first.

Arguments:

| Name | Type | Constraints | Required | Description |
| --- | --- | --- | --- | --- |
| `dataset` | dataset id | registered corpus | optional (default `openstack`) | Which corpus to query. |
| `start_time` | datetime string | window start | required | Window start. |
| `end_time` | datetime string | after `start_time`, exclusive | required | Window end. |
| `type` | `(field, exact value)` | field is `service`/`entity_id`/`event_id`/`template`/`severity` | required | The value to follow. |
| `bucket` | `second`/`minute`/`hour`/`day`/`week`/`month` | — | optional | Bucket width; omit and the window picks the finest bucket that keeps the series to about 100 points. |
| `category` | severity string | corpus's own casing | optional | Filter by severity. |
| `group_by` | `severity` | — | optional | Return one series per severity level that occurs. |

Returns: `kind=trend` rows `{bucket, [severity,] count, prev_count, trend}` for **every** bucket in the window, with `count` 0 where nothing was logged. `query_stats.notes` names the bucket used and summarizes consecutive non-zero bucket runs with their totals.

Limits: a window that would need more than 240 points is refused with a coarser bucket suggested.

Empty/refused: an unknown value returns zeros across the window, not an error.

### `count_by_category_tool`

Purpose and when: count log rows per severity in a time window, overall or broken down by another field. It is the cheapest aggregate — it never reads message text — so use it first to find where the problem is before paying to search text.

Arguments:

| Name | Type | Constraints | Required | Description |
| --- | --- | --- | --- | --- |
| `dataset` | dataset id | registered corpus | optional (default `openstack`) | Which corpus to query. |
| `start_time` | datetime string | window start | required | Window start. |
| `end_time` | datetime string | after `start_time`, exclusive | required | Window end. |
| `type` | `general`/`service`/`entity_id`/`template`/`event_id` | `event_id` only where the corpus has one | optional (default `general`) | Breakdown dimension; `general` counts by severity only. |
| `category` | severity string | corpus's own casing | optional | Filter by severity. |
| `limit` | integer | 1–200 (default 50) | optional | Most rows to return. |

Returns: `kind=aggregate` rows `{[<type>,] severity, count}` computed over every matching row — a measured frequency for the window and filters. `query_stats.approximate` marks a corpus without exact counts.

Limits: the row cap is `limit`; the count itself is over every matching row.

Empty/refused: a window outside the data returns nothing, which proves nothing; take windows from `list_datasets_tool`.

### `sweep_chunks_tool`

Purpose and when: examine a whole remote corpus in graded passes and rank the chunks that look relevant. **Last resort** — use it only when the user insists on evidence from the entire corpus and a bounded query will not do.

Arguments:

| Name | Type | Constraints | Required | Description |
| --- | --- | --- | --- | --- |
| `dataset` | dataset id | a chunk-sweepable corpus (default `otel_s3`) | optional | Which corpus to sweep. |
| `question` | string | 3–500 chars | required | What the investigation is trying to establish. |
| `max_promotions` | integer | 0–4 (default 0) | optional | Chunks to read message text for; each costs about eleven minutes. |
| `start_at` | integer | ≥ 0 (default 0) | optional | Chunk index to resume from (a previous sweep's cursor). |
| `confirm_cost` | boolean | (default false) | optional | Must be true to run; false returns a cost estimate. |

No time window, deliberately: it addresses the corpus by row group and is bounded by its own budgets.

Returns: `kind=search` one `{coverage, progress, cursor, notes, trace, sampling}` row, then `{ranked_chunk}` and `{digest}` rows. `trace` lists every stage and the score behind each decision; `sampling` gives each sample's examined fraction and a likely **range** for its match rate.

Limits: `max_promotions` caps text reads; each promotion is about eleven minutes. A profiled chunk was counted, not read.

Empty/refused: always call first with `confirm_cost=false` to get an estimate, show it to the user, and call again with `confirm_cost=true` once they agree. Report the returned coverage — a sweep can miss evidence and must not imply otherwise.

### `investigate_chunk_tool`

Purpose and when: read one named chunk's message text and return counts, templates and verbatim lines. Use it on a chunk a sweep ranked highly, when the ranking alone is not enough and the investigation needs quotable lines.

Arguments:

| Name | Type | Constraints | Required | Description |
| --- | --- | --- | --- | --- |
| `dataset` | dataset id | a chunk-sweepable corpus (default `otel_s3`) | optional | Which corpus to read. |
| `chunk_id` | string | 3–64 chars, pattern `part_000:412-412` | required | Chunk identifier from a sweep result. |
| `question` | string | ≤ 500 chars (default empty) | optional | What the investigation is after. |

Returns: `kind=search` one row with the chunk's counts, templates, verbatim lines and `coverage`.

Limits: costs about eleven minutes; it indexes the chunk, summarizes it, then drops it.

Empty/refused: reading one chunk says nothing about the rest of the corpus — carry the returned coverage into the answer.

### `search_logs_tool`

Purpose and when: search log message text on an indexed corpus and return the best-matching example lines, optionally for one severity and within a time window. Locate the problem with `count_by_category_tool` first, then read the actual lines here.

Arguments:

| Name | Type | Constraints | Required | Description |
| --- | --- | --- | --- | --- |
| `dataset` | dataset id | an indexed corpus | optional (default `openstack`) | Which corpus to search. The raw remote corpus has no index and is refused; use `otel_slice`. |
| `query_text` | string | 1–500 chars | required | Text or concept to find in message bodies. |
| `category` | severity string | corpus's own casing | optional | Filter by severity; do not put a severity in `query_text` as a filter. |
| `limit` | integer | 1–50 (default 10) | optional | Most example rows; the totals still count every match. |
| `start_time` | datetime string | optional; with `end_time`, or neither | optional | Window start. |
| `end_time` | datetime string | optional; with `start_time` | optional | Window end, exclusive. |

Returns: `kind=search` at most `limit` example rows `{record_id, ts, severity, service, entity_id, message, relevance, and event_id where the corpus has one}`, plus `search_mode`, `total_matches` (rows containing **every** search word under the same severity and window), `total_matches_any` (rows containing **at least one** — the pool the rows are ranked from), `truncated`, and the executed query.

Limits: `limit`. Matching is BM25 on the words, always; where a corpus has template embeddings, `search_mode` is `template` and lines semantically close to the query's event shape are fused in (those lines are not in the counts, which stay lexical).

Empty/refused: the raw remote corpus is refused (no index). On `otel_slice`, an uncovered window is indexed automatically first, which takes minutes — narrow it with aggregates before searching. Zero rows means nothing matched in that scope, not that the event never happened. **Examples never measure frequency.**

### `otel_index_tool`

Purpose and when: report what the in-place OTel full-text index covers, index a time window so it can be searched, or release the index and reclaim its disk. You rarely need it — `search_logs_tool` on `otel_slice` indexes its window automatically. Use it to see what is searchable, to build a window ahead of several searches, or to give the disk back.

Arguments:

| Name | Type | Constraints | Required | Description |
| --- | --- | --- | --- | --- |
| `start_time` | datetime string | optional; with `end_time`, or neither | optional | Window start to make searchable. |
| `end_time` | datetime string | optional; with `start_time` | optional | Window end, exclusive. |
| `release` | boolean | cannot combine with a window (default false) | optional | Drop the index and return its disk. |

Returns: `kind=catalog`, one row — `exists`, `window_start`, `window_end`, `severities`, `covers`, `index_bytes`, `built`. A completed build adds `build_seconds`, `indexed_rows`, `bytes`, `reclaimed_bytes` and `memory_ceiling`; a release returns `released`, `freed_bytes` and `db_bytes`.

Limits: a build takes minutes and replaces the previous index, so only one index's worth of disk is held. With no arguments it builds nothing.

Empty/refused: `exists: false` means nothing is searchable on `otel_slice` yet. After a release, the next uncovered search rebuilds.

### `top_in_category_by_service_tool`

Purpose and when: rank a field's values (default: the emitting service) by log volume in a time window, optionally for one severity. Use it to locate the noisiest components before searching text.

Arguments:

| Name | Type | Constraints | Required | Description |
| --- | --- | --- | --- | --- |
| `dataset` | dataset id | registered corpus | optional (default `openstack`) | Which corpus to query. |
| `start_time` | datetime string | window start | required | Window start. |
| `end_time` | datetime string | after `start_time`, exclusive | required | Window end. |
| `field` | canonical field | default `service` | optional | Field to rank. |
| `category` | severity string | corpus's own casing | optional | Filter by severity. |
| `limit` | integer | 1–100 (default 5) | optional | Most values to return. |

Returns: `kind=aggregate` rows `{<field>, count}`, the top `limit` by count over every row in the window — measured counts of log entries.

Limits: `truncated` means more values exist.

Empty/refused: an empty window returns no rows; take the window from `list_datasets_tool`.

### `compare_periods_tool`

Purpose and when: compare per-value row counts between a baseline window and a recent window, to see what changed. It scans both windows, so it costs roughly twice a single count. It is a quick "what changed fastest" view, ordered by volume change and truncated, so it misses large, flat values — do not rank from it alone.

Arguments:

| Name | Type | Constraints | Required | Description |
| --- | --- | --- | --- | --- |
| `dataset` | dataset id | registered corpus | optional (default `openstack`) | Which corpus to query. |
| `baseline_start` | datetime string | window start | required | Baseline window start. |
| `baseline_end` | datetime string | after `baseline_start`, exclusive | required | Baseline window end. |
| `recent_start` | datetime string | window start | required | Recent window start. |
| `recent_end` | datetime string | after `recent_start`, exclusive | required | Recent window end. |
| `field` | canonical field | default `service` | optional | Field whose values are compared. |
| `limit` | integer | 1–100 (default 5) | optional | Most values to return. |

Returns: `kind=aggregate` rows `{<field>, severity, baseline_count, recent_count, volume_change, percentage_change}`, ordered by `volume_change` — measured counts for the top `limit` values.

Limits: `limit`; ordered by change and truncated.

Empty/refused: an empty window contributes zero counts.

### `get_data_tool`

Purpose and when: list the most common values of a field in a time window, ranked by row count. Use it to discover a valid filter value (an exact service, template or entity id) before requesting a trend or filtering. Do not guess a service name.

Arguments:

| Name | Type | Constraints | Required | Description |
| --- | --- | --- | --- | --- |
| `dataset` | dataset id | registered corpus | optional (default `openstack`) | Which corpus to query. |
| `start_time` | datetime string | window start | required | Window start. |
| `end_time` | datetime string | after `start_time`, exclusive | required | Window end. |
| `type` | canonical field | — | required | Field whose values to list. |
| `limit` | integer | 1–200 (default 50) | optional | Most values to return. |

Returns: `kind=catalog` rows `{value, count}` — for discovery, not a frequency analysis.

Limits: `limit`.

Empty/refused: fields whose distinct set is effectively unbounded, such as trace ids on the OTel corpus, are refused with an alternative.

### `get_log_by_line_id_tool`

Purpose and when: fetch one complete log row by its primary-key id. Use it to resolve a `Line ID: n (dataset)` reference (what the chat box inserts for a picked log line) into the whole record. Ids are unique only within one corpus, so pass both the id and the dataset.

Arguments:

| Name | Type | Constraints | Required | Description |
| --- | --- | --- | --- | --- |
| `dataset` | dataset id | a corpus with a primary key | optional (default `openstack`) | Which corpus to read. |
| `line_id` | integer | ≥ 1 | required | Primary-key id of the row to fetch. |

Returns: `kind=lookup`, zero or one full row (`record_id` plus the canonical fields), never clipped.

Limits: none.

Empty/refused: zero rows means no such id in that dataset. The raw OTel corpus has no primary key and refuses, with a pointer to the datasets that do.

### `get_nearby_events_tool`

Purpose and when: return the log lines just before and after one anchor line, for the engineer's before/after timeline. Use it on a search hit's `record_id` (or a `Line ID: n (dataset)` reference) after `search_logs_tool` or `get_log_by_line_id_tool` found the line.

Arguments:

| Name | Type | Constraints | Required | Description |
| --- | --- | --- | --- | --- |
| `dataset` | dataset id | a corpus with a record id (`openstack`, `bgl`) | optional (default `openstack`) | Which corpus to read. |
| `line_id` | integer | ≥ 1 | required | Record id of the anchor line. |
| `before` | integer | 0–50 (default 5) | optional | Lines to return before the anchor. |
| `after` | integer | 0–50 (default 5) | optional | Lines to return after the anchor. |
| `same_entity` | boolean | (default false) | optional | Only lines sharing the anchor's `entity_id` (process on OpenStack, node on BGL). |
| `same_request` | boolean | OpenStack only (default false) | optional | Only lines sharing the anchor's `request_id`. |

Returns: `kind=context` rows ordered by `record_id` — `record_id`, `ts`, `severity`, `service`, `entity_id`, `request_id` (OpenStack), `message`, `template`, `offset` (negative before, 0 for the anchor, positive after). With `same_request`, `query_stats.notes` also summarizes the whole request (rows, first and last timestamp, components).

Limits: `before`/`after` 0–50 each. Neighbours follow record-id (file) order, not a time window.

Empty/refused: an unknown `line_id`, or an anchor with no value for the flagged field, is a success with empty or anchor-only data and a note — report it, not "nothing happened". Datasets without a record id (the OTel tiers) are refused; `same_request` is refused where the corpus records no request id. **Context rows are examples, never a frequency.**

### `count_by_team_tool`

Purpose and when: count log entries per owning team and severity in the local datasets, joining each row's service (Component) to the `team_ownership` table by corpus and component. Use it to group recurring problems by team, or to say which team owns most of the errors. For a breakdown by service use `count_by_category_tool`.

Arguments:

| Name | Type | Constraints | Required | Description |
| --- | --- | --- | --- | --- |
| `datasets` | list of local dataset ids | min 1; local datasets only | optional | Datasets to count; omit to count every local dataset. |
| `start_time` | datetime string | optional; with `end_time`, or neither | optional | Window start. |
| `end_time` | datetime string | optional; with `start_time` | optional | Window end, exclusive. |
| `category` | severity string | corpus's own casing | optional | Filter by severity. |

Returns: `kind=aggregate` rows `{dataset, team, severity, count}`, ordered by count, with no row cap. Services with no owner count as team `unowned`, so team totals equal the dataset totals.

Limits: local datasets only; no row cap.

Empty/refused: the remote corpus, an unloaded dataset and a missing ownership table are refused with a suggestion. A severity absent from some datasets counts only the others and says so in `query_stats.notes`.

### `rank_priorities_tool` (MCP only)

Purpose and when: rank the biggest problems in each dataset from measured counts in two adjacent time windows. Use it instead of ranking counts yourself: it takes no numbers from the caller, reads every count from SQL and scores with the pipeline's formula, so the order is checkable. The pipeline's `answer_node` adds its own ranking record, so the pipeline does not offer this tool.

Arguments:

| Name | Type | Constraints | Required | Description |
| --- | --- | --- | --- | --- |
| `datasets` | list of dataset ids | each ranked on its own; omit for every local dataset | optional | Datasets to rank. |
| `group_by` | `template`/`service`/`team` | `team` is local only | optional (default `service`) | What a ranked row is. |
| `baseline_start` | datetime string | window start | required | Baseline window start. |
| `baseline_end` | datetime string | after `baseline_start`, ≤ `recent_start` | required | Baseline window end, exclusive. |
| `recent_start` | datetime string | window start | required | Recent window start. |
| `recent_end` | datetime string | after `recent_start`, exclusive | required | Recent window end. |
| `limit` | integer | 1–50 (default 10) | optional | Most ranked rows per dataset. |

The two windows must not overlap: a value's count is the sum of its two window counts, and the trend compares them.

Returns: `kind=derived` rows, one per ranked value — `dataset`, `rank`, the grouped value, `non_info_rows`, `error_rows`, `volume_share_pct`, `error_share_pct`, `trend_up`, `breadth`, `score` — at most `limit` rows per dataset, while `total_matches` says how many values were ranked in all. A row is a ranking, not a frequency: quote its `non_info_rows` and the queries in `query`, never the score alone.

Limits: `limit` per dataset. A remote dataset is ranked only with `group_by='service'` or `'template'`, in windows of at most 300 seconds, and its counts are approximate.

Empty/refused: empty data means no non-informational rows in either window for that dataset. `query_stats.notes` names the formula, the terms used, and any value dropped because a window hit its group cap.

### `get_trace_logs_tool` (MCP only)

Purpose and when: return every log line that carries one trace id, across all the services that wrote it, oldest first, to follow a failing request from the frontend to the service that broke. Use it on the `entity_id` (trace id) of a line found on the OTel corpus, with a window centred on that line.

Arguments:

| Name | Type | Constraints | Required | Description |
| --- | --- | --- | --- | --- |
| `dataset` | dataset id | a remote dataset with trace ids (default `otel_s3`) | optional | Which corpus to read the trace from. |
| `trace_id` | string | 32 lowercase hex characters | required | The trace id. |
| `start_time` | datetime string | window start; window at most 300 s | required | Window start. |
| `end_time` | datetime string | after `start_time`, ≤ 300 s later | required | Window end, exclusive. |
| `limit` | integer | 1–200 (default 100) | optional | Most lines to return, oldest first. |

Returns: `kind=context` rows ordered by time — `ts`, `severity`, `service`, `span_id`, `message`, `t_plus_s` (seconds since the trace's first returned line). `query_stats.notes` lists the services seen.

Limits: window at most 300 seconds; `limit`.

Empty/refused: only lines that carry a trace id appear, so a service that sets none is missing even if it logged during the request. Empty data means no line in the window has this id (a wrong id, a window that misses the trace, or services that set no id), not that the request did nothing. Other datasets are refused; use `count_by_category_tool` to count instead. **Rows are examples of one request, never a frequency.**

---

Hand-written from `src/backend/pipeline/tools/specs.py` and `src/backend/pipeline/tools/schemas.py` at commit `0b0e266`; keep it in sync when tools change.
