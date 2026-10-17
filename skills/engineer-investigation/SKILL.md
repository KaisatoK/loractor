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
   (`category`) and, when a time-filtered search tool is available, by the time
   window. Keep the `record_id` (the line id), timestamp, service and template of
   the best hits.
2. **Nearby events.** When a nearby-events tool is available (planned:
   `get_nearby_events_tool`, events around a `line_id`), call it on the most
   relevant hit. Until then, approximate from the hit's `record_id` (search
   returns it; lines follow the original file order): call
   `get_log_by_line_id_tool` with `line_id` = `record_id - 1` and `record_id + 1`
   (more neighbours if the budget allows), and use
   `get_trend_tool` for the hit's template (or service) over the window. Say that the timeline
   is approximate (adjacent lines, not a time-bounded slice).
3. **Isolated or recurring?** One `count_by_category_tool` (type `template` or
   `service`) over the whole dataset window, so the answer can say whether this
   error appears once or many times. This is a database count, not the number of
   search hits.
4. Optional, when a hypothesis needs it: one query that could contradict it
   (a counterexample: the same error without the suspected preceding event).

## Answer must contain
- The failing component, event and exact error message(s) with line ids.
- An ordered before/after timeline from the retrieved rows.
- The table of the matching lines and their neighbouring events.
- A possible cause, labelled as a hypothesis unless the rows confirm it. State
  the hypothesis once, where hypotheses are listed; do not repeat it in the
  narrative.
- One or two concrete diagnostic checks (what to inspect, where, and why), each
  saying which result would confirm the cause and which would refute it.

## Never
- Report the number of search hits as a frequency.
- Present a cause the rows do not show as a fact.
