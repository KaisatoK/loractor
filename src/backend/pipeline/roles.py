"""Role objectives shared by the API request contract and the prompt builders.

Each role asks a different question of the same logs, so each needs different
evidence and a different next step, not just a different answer length. The
evidence plan for each role lives in its playbook skill
(``skills/<ROLE_SKILLS[role]>/SKILL.md``), which is injected into the
``find_evidence`` prompt and served to MCP clients by ``read_skill``. The
objective below is the answer-side contract: what the answer must contain and
what it must never claim.
"""

from backend.pipeline.state import RoleName

# Shared wording rules for every role (from the challenge brief).
_COMMON = (
    " Count matching log entries accurately and call them log entries, never "
    "incidents. A few retrieved examples never establish how often something "
    "happens; only database counts do. Label every explanation that the logs do "
    "not confirm as a hypothesis, and say what remains unknown."
)

ROLE_OBJECTIVES: dict[RoleName, str] = {
    "engineer": (
        "Help the engineer investigate a specific error: what failed, in which "
        "component, and what happened just before and after it. Ground the answer "
        "in the matching error messages (with line ids and timestamps) and the "
        "nearby events around them, explain a possible cause as a labelled "
        "hypothesis, and name the single next diagnostic check, such as the "
        "component, request id or time window to inspect next. Structure the "
        "narrative as: What failed; Timeline before and after; Possible cause in "
        "one or two sentences. State each hypothesis and each next check once, as a "
        "finding or recommendation, not again in the narrative." + _COMMON
    ),
    "team_lead": (
        "Help the team lead see which problems recur, how often, and which "
        "components or teams are involved. Use database counts and a trend over a "
        "stated period, grouped by event template, component and owning team "
        "(from the team-ownership table), with at most one example line per "
        "pattern for illustration. Recommend what the team should investigate "
        "first and why, based on the measured frequency and trend. Structure the "
        "answer as: Period and scope; Recurring patterns (counts); Trend; Teams "
        "and components involved; Investigate first." + _COMMON
    ),
    "manager": (
        "Help the manager decide which observed problems deserve attention first "
        "and what should happen next. Give a short, prioritized list based only on "
        "measured frequency, severity share, trend direction and the number of "
        "affected components or teams, and state the basis of the ranking. For "
        "each priority, give one concrete action and the owning team. Do not "
        "invent financial losses, downtime, customer impact or effort estimates. "
        "Structure the answer as: Top priorities; Why this order (measured basis); "
        "Risks supported by the data; Actions and owners; What is not known."
        + _COMMON
    ),
}

# The playbook skill each role's investigation follows.
ROLE_SKILLS: dict[RoleName, str] = {
    "engineer": "engineer-investigation",
    "team_lead": "recurring-patterns",
    "manager": "risk-prioritization",
}
