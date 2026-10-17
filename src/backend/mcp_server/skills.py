"""The investigation skills (``skills/*/SKILL.md``) as the MCP server serves them.

The files are the one source of the evidence plans and answer rules; the
pipeline injects the same text into its prompts (``pipeline/playbooks.py``).
"""

from __future__ import annotations

import re
from pathlib import Path

from backend.mcp_server.config import MCP_SKILLS_ROOT

_SKILLS_ROOT = (
    Path(MCP_SKILLS_ROOT)
    if MCP_SKILLS_ROOT
    else Path(__file__).resolve().parents[3] / "skills"
)


def skill_files() -> dict[str, Path]:
    if not _SKILLS_ROOT.is_dir():
        raise RuntimeError(f"Skills directory not found: {_SKILLS_ROOT}")
    paths = {
        p.parent.name: p for p in _SKILLS_ROOT.glob("*/SKILL.md") if p.is_file()
    }
    # Deterministic order, the hypothesis workflow first (glob order is not).
    ordered = sorted(paths, key=lambda name: (name != "log-investigation", name))
    return {name: paths[name] for name in ordered}


def skill_body(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    return re.sub(r"^---\s*\n.*?\n---\s*\n", "", text, count=1, flags=re.DOTALL)


def list_skill_infos() -> list[dict[str, str]]:
    result = []
    for name, path in skill_files().items():
        text = path.read_text(encoding="utf-8")
        description = ""
        match = re.search(r"^description:\s*(.+)$", text, re.MULTILINE)
        if match:
            description = match.group(1).strip().strip('"')
        result.append({"name": name, "description": description})
    return result


def read_skill_text(name: str) -> str:
    if name not in skill_files() or name in {".", ".."} or "/" in name or "\\" in name:
        raise ValueError("unknown skill")
    return skill_body(skill_files()[name])
