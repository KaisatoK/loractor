# Known limitations

What the current build does not do, grouped by cause. Each entry names the effect; the measurement-backed version of this list is the README's [Limitations](../README.md#limitations) section, which is authoritative.

## Data and fields

- **`request_id` exists only on OpenStack.** Request-level correlation (`same_request`) is refused on other datasets; use `same_entity` there.
- **The local corpora have no `event_id`.** loghub publishes one only for its 2,000-line samples, so recurring patterns are counted by `template`.
- **Severity vocabularies differ per corpus and casing matters.** OpenStack and BGL use upper-case levels (`ERROR`), the OTel corpus lower-case (`error`). Filtering with the wrong casing silently matches nothing.
- **The team-ownership table is fictional.** `src/backend/data/team_ownership.csv` is an invented `dataset,component,team` mapping, so every "owning team" in an answer is fictional until the real table replaces it. Unmapped components count as `unowned`.
- **Bundled samples vs full corpora.** The repository ships 2,000-line samples; the full BGL and OpenStack corpora come from separate loghub downloads. Live corpus tests assume the full corpora and fail row-count assertions against the samples.
- **Remote OTel aggregates are approximate.** They are computed over pruned row groups rather than the full corpus, and are labelled `approximate: true`.

## Measurement and wording

- **Counts are log entries, never "incidents".** Matching lines are not automatically separate incidents.
- **Search examples never establish frequency.** Only aggregate, trend and the code-derived ranking measure; search, context, lookup and catalog rows are examples or metadata.
- **The sweep is a sampled pass, not exhaustive.** At Extreme effort it covers roughly 7% of one of fifty files in ten minutes; a chunk that was profiled was counted, not read.
- **The OTel in-place index covers problem severities only** (`error`, `fatal`, `warn`), and only one window at a time — so an info-level full-text search on `otel_slice` cannot be served even inside the covered window.

## Operations

- **The SereneDB host port can drift.** A Docker container's mapped port can change when it is recreated; `SERENEDB_PORT` in `.env` then goes stale (`docker ps` shows the current mapping). The native server always listens on 7890.
- **The memory ceiling does not survive restarts.** A freshly started server reports a much higher `memory_limit` than the budget; it must be re-applied (`bootstrap.sh` does, and every index build holds it).
- **Building the OTel index takes minutes and is a flat cost.** A one-second window measured about 446s; a narrower window is no cheaper. A search outside the covered window rebuilds.
- **Remote Parquet reads can fail transiently** (a "Could not resolve hostname" that succeeds minutes later with no change). Retry before investigating the caller.
- **In-flight investigation runs are in-process.** A single uvicorn worker owns the background run registry, so a backend restart loses any in-flight run (persisted chat history survives).

## Testing and coverage

- **The live suites are gated and skipped by default.** `RUN_OPENSTACK_LIVE`, `RUN_BGL_LIVE` and `RUN_OTEL_LIVE` need the corpora and a reachable database.
- **The live route, claims and confidence suites are stochastic.** Run them alone (`bash ./routes.sh`, `bash ./claims.sh`, `bash ./confidence.sh`) — never inside the full suite — and treat a flipped row as a product signal to re-run once, not to re-baseline.

## Not built

- **The request-trace and hypothesis-test tools (T8–T12) were dropped** (see `docs/tool_requests.md`). The answer's hypotheses are model-written judgements over measured evidence, not deterministic tests. The ranking tool that was part of that group landed as the server-only `rank_priorities_tool`.

The authoritative, measurement-backed list — with the exact errors and figures behind each entry, and the workarounds — is in the README's [Limitations](../README.md#limitations) section.
