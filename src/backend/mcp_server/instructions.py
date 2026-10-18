"""The guidance an MCP client shows its model when it connects.

Rendered, not retyped: each rule is read from the module that owns it (role ->
skill map, which result kinds are frequencies, the shared answer rules, the scope
policy), so the instructions cannot drift from what the pipeline does. Only the
connective prose lives here. A test checks that every tool the text names exists.
"""

from __future__ import annotations

from backend.pipeline import priority
from backend.pipeline.prompts.builders import QUERYABLE_TARGETS
from backend.pipeline.roles import COMMON_ANSWER_RULES, OVERVIEW_ROLES, ROLE_SKILLS
from backend.pipeline.tools.envelope import MEASURED_KINDS


def _role_plans() -> str:
    return "; ".join(f"{role}: {skill}" for role, skill in ROLE_SKILLS.items())


def build_instructions() -> str:
    overview = " and ".join(sorted(OVERVIEW_ROLES))
    engineer_like = ", ".join(sorted(set(ROLE_SKILLS) - OVERVIEW_ROLES))
    measured = " and ".join(sorted(MEASURED_KINDS))
    return f"""\
Evidence tools over SereneDB log corpora, for three roles: an engineer investigating one error, a team lead looking at recurring problems, and a manager deciding what to prioritise.

How to work
1. Call list_datasets_tool first: it measures each dataset's rows, span, levels and a ready window to take windows and severities from, not guess.
2. Follow the user's role plan, read with read_skill (or the resource skill://<name>/SKILL.md): {_role_plans()}. list_skills says when each applies, including the 'why' hypothesis workflow.
3. Find a service, template or entity id with get_data_tool before filtering.
4. A request needs a target ({"; ".join(QUERYABLE_TARGETS)}); if the user named none, ask before calling tools -- except a dataset-wide overview, valid for {overview}, not {engineer_like}.
5. Rank problems for {overview} with rank_priorities_tool, not by comparing counts yourself: it scores measured SQL counts with {priority.FORMULA}.

Reading results
- Only {measured} results are frequencies; search, context, lookup and catalog rows are examples or metadata, and derived rows are computed from measured counts.
- Every result carries its executed `query`, `filters`, `limit`, `returned`, `total_matches` and `truncated`; cite the query behind every number and say when truncated.
- status "refused" is an answer, not an error: `reason` says what was wrong, `suggestion` what would be accepted; follow it and retry.
- {COMMON_ANSWER_RULES.strip()}
"""
