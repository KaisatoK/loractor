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
2. **Measured frequency and severity.** Call `count_by_team_tool` for local
   datasets over the scope (and `count_by_dataset_tool` when comparing whole
   datasets). It returns measured counts by dataset, owning team and severity;
   use those counts for the ranking. The team tool joins `team_ownership` and
   labels unmapped components `unowned`. For remote OTel, use
   `count_by_category_tool` by service with a bounded window instead.
3. **Trend direction.** Count each half of the scope separately with
   `count_by_category_tool` (the same `type` and severity filter for both halves),
   so every candidate has a count in both windows and the ranking can compare them;
   `get_trend_tool` for the top candidates shows the shape over time. If a trend is
   refused for too many buckets, take the suggested coarser bucket; a split period
   must split on whole buckets. `compare_periods_tool` is a quick "what changed
   fastest" view, but it is ordered by volume change and truncates, so it misses
   large, flat values -- do not rank from it alone.
4. **Breadth.** From the team-count rows, use the number of teams and
   components represented by each candidate. Do not infer breadth from the
   number of returned examples; the measured team aggregate is the source for
   owning-team coverage.
5. Optional: one `search_logs_tool` example (limit 1) for the top priority so the
   manager can see what it is.

## Answer must contain
- At most three priorities, in order, each with its measured basis (count,
  severity share, trend direction, components/teams affected).
- The reason for the order, in one sentence. When a computed priority ranking is
  present, follow its order and say it was computed from the counts.
- When more than one dataset is in scope, give each dataset its own ranking; do
  not rank datasets against one another (their windows and sizes differ).
- At most one table.
- One concrete next action per priority and the owning team.
- What the data does not tell us (cause, impact) in one line.

## Never
- Invent financial loss, downtime, customer impact, SLAs or effort estimates.
- Rank by examples instead of counts.
- Count a problem total that leaves out WARNING: problem severity is every level
  above informational, WARNING/warn included.
- Add counts up yourself: state a total only when a query returns it.
