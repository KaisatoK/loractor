---
name: risk-prioritization
description: Evidence plan for a MANAGER deciding what to prioritize - rank observed problems by measured frequency, severity, trend and breadth, with an action and owning team for each. Use when the user's role is manager.
---

# Risk-prioritization playbook (manager)

## Question this role asks
Which observed problems deserve attention first, and what should happen next?

## Evidence to collect (in this order)
1. **Scope.** If the user named none, call `list_datasets_tool` and use the
   dataset's `window_start`..`window_end` (every row, measured on the call) as
   the whole-dataset window, and say so. Its `levels` show which severities
   actually occur.
2. **Measured frequency and severity.** `count_by_category_tool` with type
   `service` (or a team-count tool when available, planned: `count_by_team_tool`)
   over the scope, for every severity level above informational that actually
   occurs in the data (the catalog's `levels`, or a count by level, tells you
   which levels exist; never assume a level is present or absent). These counts drive the
   ranking. When the question spans several datasets, or asks which dataset
   needs attention first, `count_by_dataset_tool` gives measured severity counts
   for each whole local dataset in one call.
3. **Trend direction.** `compare_periods_tool` (first half vs second half of the
   scope) or `get_trend_tool` for the top two or three candidates, to see which
   problems are growing. If a trend is refused for too many buckets, take the
   suggested coarser bucket; a split period must split on whole buckets.
4. **Breadth.** From the counts, how many components (and owning teams, from the
   team-ownership mapping) each problem touches.
5. Optional: one `search_logs_tool` example (limit 1) for the top priority so the
   manager can see what it is.

## Answer must contain
- At most three priorities, in order, each with its measured basis (count,
  severity share, trend direction, components/teams affected).
- The reason for the order, in one sentence. When a computed priority ranking is
  present, follow its order and say it was computed from the counts.
- At most one table.
- One concrete next action per priority and the owning team.
- What the data does not tell us (cause, impact) in one line.

## Never
- Invent financial loss, downtime, customer impact, SLAs or effort estimates.
- Rank by examples instead of counts.
