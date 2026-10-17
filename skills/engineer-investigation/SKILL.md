---
name: engineer-investigation
description: Evidence plan for an ENGINEER investigating a specific error - find the failing lines, what happened just before and after, a possible cause, and the next check. Use when the user's role is engineer.
---

# Engineer investigation playbook

## Question this role asks
What failed, in which component, and what happened just before and after?

## Evidence to collect (in this order)
1. **Find the error lines** with full-text search (`search_logs_tool`): use the
   error wording, component or event the user named; restrict by severity
   (`category`) and, when the user named a period, by the time window
   (`start_time` and `end_time` together). Keep the `record_id` (the line id),
   timestamp, service and template of the best hits. The result's
   `total_matches` counts every matching line, so say "n shown of
   total_matches"; it is the number of matching log entries, not a frequency
   over time (step 4 measures that).
2. **Nearby events.** Call `get_nearby_events_tool` on the most relevant hit:
   `line_id` = its `record_id`, the same `dataset`. The rows are the lines just
   before and after it in file order, each with an `offset` (negative before,
   0 for the hit, positive after), so they give the before/after timeline
   directly. To follow one process or node, set `same_entity=true`; on a dataset
   whose fields include `request_id` (listed per dataset under `<datasets>`),
   `same_request=true` follows one API request. Read the tool's request-context
   summary: if the request id covers thousands of lines across the dataset, it
   is a service's periodic-task context, not a user request; in that case,
   follow the actual API request on the unfiltered neighbouring rows instead.
   The tool refuses `same_request` on a dataset without that field; use
   `same_entity` there.
   When budget allows, one unfiltered call plus one filtered call shows both what
   the system was doing at the time and what the failing process or request did.
   A note in the result explains an empty or anchor-only timeline; report it
   rather than treating it as "nothing happened".
3. **Measure local spread when the failure is clustered.** If the anchor's
   second or minute contains several `ERROR`/`FATAL` rows, call
   `count_by_category_tool` with `type="entity_id"` over that same short window.
   Use a `limit` large enough to include every relevant entity, rather than
   accepting a top-10 view. This measures whether the event is isolated to one
   process/node or spread across processors, nodes or components; do not infer
   the spread from the number of context rows.
4. **Isolated or recurring?** Call `count_by_category_tool` once with
   `type="template"` or `type="service"` over the whole dataset window
   (`window_start`..`window_end` from `list_datasets_tool`, when you do not
   have it yet), so the answer can say whether this error appears once or many
   times. This is a database count, not the number of search hits.
5. Optional, when a hypothesis needs it: one query that could contradict it
   (a counterexample: the same error without the suspected preceding event).

## Answer must contain
- The failing component, event and exact error message(s) with line ids.
- An ordered before/after timeline from the retrieved rows. When a result named
  `timeline` is present, show that table (it is ordered and timed in code, with
  each line's offset and seconds from the failing line) and do not re-sort or
  re-time the rows yourself; when none is present, say the timeline could not be
  built from the rows retrieved.
- The table of the matching lines and their neighbouring events.
- A possible cause, labelled as a hypothesis unless the rows confirm it. State
  the hypothesis once, where hypotheses are listed; do not repeat it in the
  narrative.
- One or two concrete diagnostic checks (what to inspect, where, and why), each
  saying which result would confirm the cause and which would refute it.

## Never
- Report the number of search hits as a frequency.
- Present a cause the rows do not show as a fact.
