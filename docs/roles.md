# One workflow, three roles

The same agent and the same log corpus serve all three roles. What changes is the question, the evidence that is selected, and the recommended actions — not just the length of the answer. Each role has its own playbook (a skill) that is injected into the evidence prompt and also served over MCP, so an external agent follows the same plan.

## The workflow

Every turn runs one flow: **question → evidence → recommendation**. The question is normalized and routed, the evidence stage calls the database tools, and the answer stage explains the returned evidence with a next step. The per-turn graph is described in [architecture.md](architecture.md).

## Engineer

**Question it asks.** What failed, in which component, and what happened just before and after?

**Evidence it gathers.**

- `search_logs_tool` to find the error lines — the error wording, component or event the user named, restricted by severity (`category`) and, when a period is named, by `start_time`/`end_time`. The hit's `record_id`, timestamp, service and template are kept. `total_matches` counts lines containing every search word; `total_matches_any` counts lines containing at least one (the pool the shown rows are ranked from). Neither is a frequency over time.
- `get_nearby_events_tool` on the most relevant hit (`line_id` = its `record_id`) to get the lines just before and after it in file order, each tagged with an `offset` (negative before, 0 for the hit, positive after). `same_entity=true` follows the anchor's process (OpenStack) or node (BGL); `same_request=true` follows one OpenStack request id and is refused on datasets that do not record one.
- `count_by_category_tool` with `type="entity_id"` over a short window when the failure is clustered, to measure whether it is isolated to one process/node or spread across many.
- `count_by_category_tool` with `type="template"` or `type="service"` over the whole dataset window, to say whether the error is isolated or recurring. This is a database count, not the number of search hits.
- Optionally, one query that could contradict the leading hypothesis (a counterexample).

**What the answer must contain.** The failing component, event and exact error message(s) with line ids; an ordered before/after timeline; the table of matching lines and their neighbours; a possible cause labelled as a hypothesis unless the rows confirm it; one or two concrete diagnostic checks, each stating which result would confirm the cause and which would refute it.

**Never.** Report the number of search hits as a frequency. Present a cause the rows do not show as a fact.

**Code-computed extra.** The engineer also gets a derived `timeline` record: the nearby rows ordered by record id, each with its offset and its `seconds` from the failing line. It is built in code (`src/backend/pipeline/timeline.py`), and it is row-level evidence, not a frequency — a finding that cites it is marked "row-level evidence, not a frequency".

## Team lead

**Question it asks.** Which problems recur, how often, and which components or teams are involved?

**Evidence it gathers.**

- The **period** first: if the user named none, `list_datasets_tool` gives the dataset's `window_start`..`window_end` (every row, measured on the call), and its `levels` show which severities actually occur.
- `count_by_category_tool` with type `template` (and/or `service`) over the period, for every severity level above informational that actually occurs. These database counts are the measured frequency.
- `count_by_dataset_tool` counts each whole local dataset by severity in one call, for a question across datasets.
- `count_by_team_tool` for the same local datasets and period, joining the component column to the real `team_ownership` table. Components absent from the mapping appear as `unowned`.
- `get_trend_tool` for the top one to three recurring templates or services over the same period, to show whether each is rising, flat or bursty.
- For the computed ranking, each half of the scope is counted separately with `count_by_category_tool` (the same `type` for both halves). `compare_periods_tool` is a quick "what changed fastest" view, but it is ordered by volume change and truncates, so it misses large, flat values.
- One example per pattern with `search_logs_tool` (limit 1–3), only to show what the message looks like.

**What the answer must contain.** The period and scope first, then the top recurring patterns with their counts; the trend of the top patterns; the count/trend table and the computed priority ranking when one is present; the components and owning teams involved; what the team should investigate first and why (highest measured count or clearest rising trend).

**Never.** Infer frequency from search examples. Treat each matching log entry as a separate incident. Count a problem total that leaves out WARNING (problem severity is every level above informational, WARNING/warn included). Add counts up yourself. Rank one dataset against another.

**Code-computed extra.** A derived `priority_score` record per dataset, computed in code from the measured counts (`src/backend/pipeline/priority.py`).

## Manager

**Question it asks.** Which observed problems deserve attention first, and what should happen next?

**Evidence it gathers.**

- The **scope** first (`list_datasets_tool`, as for the team lead).
- `count_by_team_tool` for local datasets over the scope (and `count_by_dataset_tool` when comparing whole datasets) for measured counts by dataset, owning team and severity. For remote OTel, `count_by_category_tool` by service over a bounded window instead.
- Trend direction: each half of the scope counted separately with `count_by_category_tool`, plus `get_trend_tool` for the top candidates. `compare_periods_tool` is a quick view, not the ranking source.
- Breadth from the team-count rows: the number of teams and components each candidate represents.
- Optionally, one `search_logs_tool` example (limit 1) for the top priority.

**What the answer must contain.** At most three priorities, in order, each with its measured basis (count, severity share, trend direction, components/teams affected); one sentence on the reason for the order; a per-dataset ranking when more than one dataset is in scope; at most one table; one concrete next action per priority and the owning team; and one line on what the data does not tell us (cause, impact).

**Never.** Invent financial loss, downtime, customer impact, SLAs or effort estimates. Rank by examples instead of counts. Count a problem total that leaves out WARNING. Add counts up yourself. Rank datasets against one another.

**Code-computed extra.** The same `priority_score` ranking as the team lead, computed in code.

## The priority score

For the team lead and the manager, `answer_node` adds a derived ranking record computed from the measured counts (never by a model):

```
score = 0.4*volume_share + 0.3*error_share + 0.2*trend_up + 0.1*breadth
```

A term no result measures is dropped and the remaining weights are renormalized. Each dataset is ranked on its own — shares use that dataset's totals and every row carries its `dataset` — so a short remote window cannot outrank a long local pattern on raw volume. The formula and the sources it was computed from appear in the answer and in the data-scope appendix. Source: `src/backend/pipeline/priority.py`.

## Answer as another role

Under each answer, the "As \<role\>" buttons run a **new investigation** for the target role — its own evidence plan, not a rewording — in the background, and cache the result per message and role. A request at a higher effort than the cached run re-runs it. The endpoints behind the buttons and the full behaviour are documented in the README's [Role switch](../README.md#role-switch) section.

## Where this behavior lives

| Concern | Source |
| --- | --- |
| Role objectives and shared answer rules | `src/backend/pipeline/roles.py` |
| Engineer playbook | `skills/engineer-investigation/SKILL.md` |
| Team-lead playbook | `skills/recurring-patterns/SKILL.md` |
| Manager playbook | `skills/risk-prioritization/SKILL.md` |
| Shared hypotheses discipline | `skills/log-investigation/SKILL.md` |
| Priority score | `src/backend/pipeline/priority.py` |
| Engineer timeline | `src/backend/pipeline/timeline.py` |

The role skills carry the authoritative wording for each role's sections ("Question this role asks", "Answer must contain", "Never"); this page summarises them.
