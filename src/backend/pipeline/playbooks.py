"""Role playbooks and the ownership mapping injected into prompts.

The playbook text is the body of ``skills/<ROLE_SKILLS[role]>/SKILL.md``. It is
injected straight into the ``find_evidence`` prompt (so following it costs no
tool call, which matters at Low effort) and the same file is served to MCP
clients through ``read_skill``, so Claude connected to the MCP server gets the
same role behavior.

The ownership mapping is read from the fictional ``team_ownership.csv``. It lets
the agent and the answer name owning teams before a database team-count tool
exists; team *totals* are never asked of the model (see ``pipeline/priority.py``
and AGENTS.md "Integrating new tools and tables").
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


@functools.lru_cache(maxsize=1)
def ownership_map() -> dict[str, str]:
    """``component -> team`` from the ownership CSV (empty if it is absent)."""
    from backend.db.ownership import read_ownership_csv

    if not Path(OWNERSHIP_CSV_PATH).is_file():
        return {}
    return {component: team for _, component, team in read_ownership_csv()}


def team_for(component: str | None) -> str:
    """The owning team of a component, or ``UNOWNED_TEAM``."""
    return ownership_map().get(component or "", UNOWNED_TEAM)


def ownership_context() -> str:
    """Compact ``team: component, ...`` lines for prompts."""
    teams: dict[str, list[str]] = {}
    for component, team in ownership_map().items():
        teams.setdefault(team, []).append(component)
    lines = [f"- {team}: {', '.join(sorted(components))}" for team, components in teams.items()]
    lines.append(f"- {UNOWNED_TEAM}: any component not listed above")
    return "Fictional team-ownership mapping (component -> owning team):\n" + "\n".join(lines)
