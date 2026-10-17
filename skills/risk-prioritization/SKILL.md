---
name: risk-prioritization
description: Evidence plan for a MANAGER deciding what to prioritize - rank observed problems by measured frequency, severity, trend and breadth, with an action and owning team for each. Use when the user's role is manager.
---

# Risk-prioritization playbook (manager)

## Question this role asks
Which observed problems deserve attention first, and what should happen next?

## Evidence to collect (in this order)
1. **Scope.** If the user named none, use the whole dataset window and say so.
2. **Measured frequency and severity.** `count_by_category_tool` with type
   `service` (or a team-count tool when available, planned: `count_by_team_tool`)
   for ERROR and for WARNING over the scope. These counts drive the ranking.
3. **Trend direction.** `compare_periods_tool` (first half vs second half of the
   scope) or `get_trend_tool` for the top two or three candidates, to see which
   problems are growing.
4. **Breadth.** From the counts, how many components (and owning teams, from the
   team-ownership mapping) each problem touches.
5. Optional: one `search_logs_tool` example (limit 1) for the top priority so the
   manager can see what it is.

## Answer must contain
- At most three priorities, in order, each with its measured basis (count,
  severity share, trend direction, components/teams affected).
- The reason for the order, in one sentence.
- One concrete next action per priority and the owning team.
- What the data does not tell us (cause, impact) in one line.

## Never
- Invent financial loss, downtime, customer impact, SLAs or effort estimates.
- Rank by examples instead of counts.
