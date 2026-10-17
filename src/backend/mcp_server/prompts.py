"""MCP prompts: ready-made investigations, one per role, and completions.

A prompt is how a client user (a slash command in Claude Code, a menu entry in
Claude Desktop) starts the workflow the chatbot runs: the role's objective and
evidence plan, the team ownership, the effort budget, the answer rules and
layout. It is assembled from the same functions the chatbot's ``find_evidence``
prompt uses; no prompt text is written here beyond the connecting sentences.
"""

from typing import Annotated

from fastmcp.prompts import PromptResult
from mcp.types import PromptReference, ResourceTemplateReference
from pydantic import Field

from backend.db.datasets import dataset_ids
from backend.mcp_server.app import mcp
from backend.mcp_server.skills import list_skill_infos
from backend.pipeline.answers import answer_layout
from backend.pipeline.effort import EFFORT_SPECS, EffortLevel
from backend.pipeline.playbooks import role_answer_rules
from backend.pipeline.prompts.builders import QUERYABLE_TARGETS, evidence_brief
from backend.pipeline.roles import OVERVIEW_ROLES, ROLE_OBJECTIVES, ROLE_SKILLS
from backend.pipeline.state import Role, RoleName

Effort = Annotated[
    EffortLevel,
    Field(
        description=(
            "How much to spend: Low (6 tool calls) to Brutal (22). The budgets "
            "are in logs://policy/effort."
        )
    ),
]


def _role(name: RoleName) -> Role:
    return {"name": name, "objective": ROLE_OBJECTIVES[name]}


def scope_policy(role_name: RoleName) -> str:
    """When this role must name a target before any tool is called."""
    if role_name in OVERVIEW_ROLES:
        return (
            "A dataset-wide overview is a valid scope for this role. If the "
            "question names no specific target, start from counts over the whole "
            "dataset (list_datasets_tool, then count_by_dataset_tool and "
            "count_by_category_tool) instead of asking."
        )
    return (
        "This role investigates something specific. A request needs a target "
        f"({'; '.join(QUERYABLE_TARGETS)}). If the question names none, ask the "
        "user for one before calling any tool."
    )


def role_prompt_text(role_name: RoleName, question: str, effort: str) -> str:
    """The full instruction for investigating ``question`` as ``role_name``."""
    return (
        evidence_brief(_role(role_name), effort)
        + "<answer rules>\n"
        + f"{role_answer_rules(role_name)}\n"
        + "</answer rules>\n"
        + "<answer layout>\n"
        + f"{answer_layout()}\n"
        + "</answer layout>\n"
        + "<scope policy>\n"
        + f"{scope_policy(role_name)}\n"
        + "</scope policy>\n"
        + "<question>\n"
        + f"{question}\n"
        + "</question>\n"
    )


def worked_investigation_text(topic: str, effort: str) -> str:
    """One topic investigated as every role in turn: the evidence and the answer
    differ per role, not only the length."""
    sections = []
    for role_name in ROLE_SKILLS:
        sections.append(
            f"## As {role_name}\n"
            + evidence_brief(_role(role_name), effort)
            + "<answer rules>\n"
            + f"{role_answer_rules(role_name)}\n"
            + "</answer rules>\n"
        )
    return (
        f"Investigate this topic as each role in turn: {topic}\n\n"
        "Give each role its own investigation with its own evidence plan and its "
        "own tool calls; do not reuse one role's rows as another's evidence. For "
        "each role report the question you asked, the tool calls and the executed "
        "queries, the key rows, the answer, and the recommended action. End with "
        "what the data does and does not establish.\n\n"
        + "\n".join(sections)
        + "\n<answer layout>\n"
        + f"{answer_layout()}\n"
        + "</answer layout>\n"
    )


def _skill_description(skill: str) -> str:
    return next(
        (s["description"] for s in list_skill_infos() if s["name"] == skill), ""
    )


def _register_role_prompt(role_name: RoleName) -> None:
    @mcp.prompt(
        name=f"investigate_as_{role_name}",
        title=f"Investigate as {role_name.replace('_', ' ')}",
        description=_skill_description(ROLE_SKILLS[role_name]),
        tags={"role", role_name},
    )
    def investigate(
        question: Annotated[
            str, Field(min_length=1, description="What the user wants to know.")
        ],
        effort: Effort = "Medium",
    ) -> PromptResult:
        return PromptResult(role_prompt_text(role_name, question, effort))


for _role_name in ROLE_SKILLS:
    _register_role_prompt(_role_name)


@mcp.prompt(
    name="worked_investigation",
    title="Worked investigation",
    description=(
        "Investigate one topic as the engineer, the team lead and the manager, "
        "each with their own evidence, answer and recommended action."
    ),
    tags={"role"},
)
def worked_investigation(
    topic: Annotated[
        str, Field(min_length=1, description="The issue or recurring pattern to investigate.")
    ],
    effort: Effort = "Medium",
) -> PromptResult:
    return PromptResult(worked_investigation_text(topic, effort))


@mcp.completion
def complete(ref, argument, context):
    """Suggest values for the arguments that have a closed vocabulary."""
    if isinstance(ref, PromptReference) and argument.name == "effort":
        values = list(EFFORT_SPECS)
    elif isinstance(ref, ResourceTemplateReference) and argument.name == "dataset_id":
        values = list(dataset_ids())
    elif isinstance(ref, ResourceTemplateReference) and argument.name == "role":
        values = list(ROLE_SKILLS)
    else:
        return None
    typed = argument.value.lower()
    return [value for value in values if value.lower().startswith(typed)]
