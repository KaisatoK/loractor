"""Role playbooks and the ownership mapping injected into prompts.

The playbook text is the body of ``skills/<ROLE_SKILLS[role]>/SKILL.md``. It is
injected straight into the ``find_evidence`` prompt (so following it costs no
tool call, which matters at Low effort) and the same file is served to MCP
clients through ``read_skill``, so Claude connected to the MCP server gets the
same role behavior.

The ownership mapping is read from the fictional ``team_ownership.csv``. It lets
the agent and the answer name owning teams, while measured team totals come
from ``count_by_team_tool``. Components not listed in the file remain
``unowned``.
"""

import functools
import re
from pathlib import Path

from backend.db.config import OWNERSHIP_CSV_PATH, UNOWNED_TEAM
from backend.pipeline.roles import ROLE_SKILLS

SKILLS_ROOT = Path(__file__).resolve().parents[3] / "skills"

_FRONTMATTER = re.compile(r"^---\s*\n.*?\n---\s*\n", re.DOTALL)


@functools.lru_cache(maxsize=None)
def role_playbook(role_name: str) -> str:
    """The playbook body for a role; raises if the skill file is missing."""
    skill = ROLE_SKILLS[role_name]  # KeyError for an unknown role is a bug
    path = SKILLS_ROOT / skill / "SKILL.md"
    return _FRONTMATTER.sub("", path.read_text(encoding="utf-8"), count=1).strip()


ANSWER_RULE_SECTIONS = ("Question this role asks", "Answer must contain", "Never")


def _section(body: str, heading: str) -> str | None:
    """The ``## heading`` block of a playbook, heading included, or ``None``."""
    match = re.search(
        rf"^## {re.escape(heading)}[ \t]*\n.*?(?=^## |\Z)", body, re.DOTALL | re.MULTILINE
    )
    return match.group(0).strip() if match else None


@functools.lru_cache(maxsize=None)
def role_answer_rules(
    role_name: str, sections: tuple[str, ...] = ANSWER_RULE_SECTIONS
) -> str:
    """The answer-side sections of a role's skill (the single source of the rules).

    Returns the requested ``sections`` (by default "Question this role asks",
    "Answer must contain" and "Never") from the same file ``role_playbook``
    reads, without "Evidence to collect". A missing section raises
    ``ValueError`` -- every section is checked, also those not requested, so a
    broken skill fails on first use whichever answer mode asks first.
    """
    body = role_playbook(role_name)
    found = {}
    for heading in ANSWER_RULE_SECTIONS:
        section = _section(body, heading)
        if section is None:
            raise ValueError(f"skill for role {role_name!r} has no '## {heading}' section")
        found[heading] = section
    return "\n\n".join(found[heading] for heading in sections)


@functools.lru_cache(maxsize=1)
def ownership_map() -> dict[str, str]:
    """``component -> team`` from the ownership CSV (empty if absent).

    ``team_for`` is retained for component-level recommendations. Measured
    team totals must come from the database team-count tool instead.
    """
    from backend.db.ownership import read_ownership_csv

    if not Path(OWNERSHIP_CSV_PATH).is_file():
        return {}
    return {component: team for _, component, team in read_ownership_csv()}


def team_for(component: str | None) -> str:
    """The owning team of a component, or ``UNOWNED_TEAM``."""
    return ownership_map().get(component or "", UNOWNED_TEAM)


def ownership_context() -> str:
    """Compact corpus-scoped ``team: component, ...`` lines for prompts."""
    from backend.db.ownership import read_ownership_csv

    by_dataset: dict[str, dict[str, list[str]]] = {}
    if Path(OWNERSHIP_CSV_PATH).is_file():
        for dataset, component, team in read_ownership_csv():
            by_dataset.setdefault(dataset, {}).setdefault(team, []).append(component)

    lines = ["Fictional team-ownership mapping, grouped by corpus:"]
    for dataset in sorted(by_dataset):
        lines.append(f"{dataset}:")
        for team in sorted(by_dataset[dataset]):
            components = ", ".join(sorted(by_dataset[dataset][team]))
            lines.append(f"- {team}: {components}")
        lines.append(f"- {UNOWNED_TEAM}: any {dataset} component not listed above")
    if not by_dataset:
        lines.append(f"- {UNOWNED_TEAM}: any component not listed above")
    return "\n".join(lines)
