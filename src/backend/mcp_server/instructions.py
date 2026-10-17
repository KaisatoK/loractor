"""The guidance an MCP client shows its model when it connects.

Rendered, not retyped: each rule is read from the module that owns it (role ->
skill map, which result kinds are frequencies, the shared answer rules, the scope
policy), so the instructions cannot drift from what the pipeline does. Only the
connective prose lives here. A test checks that every tool the text names exists.
"""

from __future__ import annotations

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
1. Call list_datasets_tool first. It measures each dataset's rows, time span, severity levels and a ready window; take windows and severities from it instead of guessing.
2. Follow the evidence plan for the user's role. Read it with read_skill (or the resource skill://<name>/SKILL.md): {_role_plans()}. list_skills says when each skill applies, including the hypothesis workflow for 'why' questions.
3. Find a service, template or entity id with get_data_tool before filtering on it.
4. A request needs a target ({"; ".join(QUERYABLE_TARGETS)}). If the user named none, ask for one before calling tools -- except that a dataset-wide overview is a valid scope for {overview}, not for {engineer_like}.

Reading results
- Only {measured} results are frequencies. Search, context, lookup and catalog rows are examples or metadata.
- Every result carries the executed `query`, its `filters`, `limit`, `returned`, `total_matches` and `truncated`. Cite the query behind every number, and say when a result is truncated.
- status "refused" is an answer, not an error: `reason` says what was wrong and `suggestion` is a request that would be accepted. Follow it and retry.
- {COMMON_ANSWER_RULES.strip()}
"""
