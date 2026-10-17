---
name: recurring-patterns
description: Evidence plan for a TEAM LEAD looking at recurring problems - counts and trends over a stated period, grouped by event, component and owning team, to decide what the team investigates first. Use when the user's role is team_lead.
---

# Recurring-patterns playbook (team lead)

## Question this role asks
Which problems recur, how often, and which components or teams are involved?

## Evidence to collect (in this order)
1. **State the period.** If the user named none, call `list_datasets_tool` and
   use the dataset's `window_start`..`window_end` (every row, measured on the
   call) as the whole-dataset window, and say so. Its `levels` show which
   severities actually occur.
2. **Recurring problems by count.** `count_by_category_tool` with type
   `template` (and/or `service`) over the period, for every severity level above
   informational that actually occurs in the data (the catalog's `levels`, or a
   count by level, tells you which levels exist; never assume a level is present or absent). These
   database counts are the measured frequency. For a question across datasets,
   `count_by_dataset_tool` counts each whole local dataset by severity in one
   call.
3. **Teams involved.** When a team-count tool is available (planned:
   `count_by_team_tool`, a join of the component column to the `team_ownership` table),
   use it. Until then, use the service counts and the team-ownership mapping
   supplied in the prompt to name the owning team of each component; do not add
   counts up yourself.
4. **Trend.** `get_trend_tool` for the top one to three recurring templates or
   services over the same period, to show whether each is rising, flat or bursty.
   `compare_periods_tool` (first half vs second half) is an alternative.
5. **One example per pattern** with `search_logs_tool` (limit 1-3), only to show
   what the message looks like.

## Answer must contain
- The period and scope first, then the top recurring patterns with their counts.
- The trend of the top patterns.
- The count/trend table, and the computed priority ranking when one is present.
- The components and owning teams involved.
- What the team should investigate first and why (highest measured count or
  clearest rising trend), as a recommendation, not a root cause.

## Never
- Infer frequency from search examples.
- Treat each matching log entry as a separate incident.
