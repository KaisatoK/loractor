---
name: recurring-patterns
description: Evidence plan for a TEAM LEAD looking at recurring problems - counts and trends over a stated period, grouped by event, component and owning team, to decide what the team investigates first. Use when the user's role is team_lead.
---

# Recurring-patterns playbook (team lead)

## Question this role asks
Which problems recur, how often, and which components or teams are involved?

## Evidence to collect (in this order)
1. **State the period.** If the user named none, use the whole dataset window
   from the schema description and say so.
2. **Recurring problems by count.** `count_by_category_tool` with type
   `event_id` (and/or `service`) for WARNING and ERROR over the period. These
   database counts are the measured frequency.
3. **Teams involved.** When a team-count tool is available (planned:
   `count_by_team_tool`, a join of `log.service` to the `team_ownership` table),
   use it. Until then, use the service counts and the team-ownership mapping
   supplied in the prompt to name the owning team of each component; do not add
   counts up yourself.
4. **Trend.** `get_trend_tool` for the top one to three recurring event ids or
   services over the same period, to show whether each is rising, flat or bursty.
   `compare_periods_tool` (first half vs second half) is an alternative.
5. **One example per pattern** with `search_logs_tool` (limit 1-3), only to show
   what the message looks like.

## Answer must contain
- The period and filters, then the top recurring patterns with their counts.
- The trend of the top patterns.
- The components and owning teams involved.
- What the team should investigate first and why (highest measured count or
  clearest rising trend), as a recommendation, not a root cause.

## Never
- Infer frequency from search examples.
- Treat each matching log entry as a separate incident.
