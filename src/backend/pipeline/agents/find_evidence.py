"""The ``find_evidence`` deep agent.

A single deepseek deep agent plans the investigation, reads the
``log-investigation`` skill, calls the seven database tools itself, and finishes by
writing a thorough free-text summary of the findings. Raw rows stay on the main
agent's own transcript; ``graph.py`` collects them from its messages after the run
(``collect_tool_results``). The ``EvidenceReport`` itself is built afterwards,
off-agent, by the tool-less JSON-mode formatter in ``evidence_report.py``, from
the agent's summary plus those captured results.
"""

import functools
import threading
from pathlib import Path

from deepagents import (
    GeneralPurposeSubagentProfile,
    HarnessProfileConfig,
    create_deep_agent,
    register_harness_profile,
)
from deepagents.backends import FilesystemBackend
from langchain.agents.middleware import (
    TodoListMiddleware,
    ToolCallLimitMiddleware,
    ToolRetryMiddleware,
)
from langgraph.graph.state import CompiledStateGraph

from backend.pipeline.effort import get_effort_spec
from backend.pipeline.middleware import (
    handle_tool_errors,
    log_tool_calls,
    make_error_middleware,
)
from backend.pipeline.models import deepseek
from backend.pipeline.prompts import FIND_EVIDENCE_SYSTEM_PROMPT
from backend.pipeline.tools.registry import get_db_tools

MAX_TOOL_RETRIES = 2

# The repo's skills directory, derived from this module's location. Rooting the
# backend here (not the repo root) means `read_file` can only reach skills —
# never `.env` or other repo files. A missing directory fails loudly rather than
# silently loading zero skills.
SKILLS_ROOT = Path(__file__).resolve().parents[4] / "skills"
if not SKILLS_ROOT.is_dir():
    raise RuntimeError(
        f"The skills directory was not found at {SKILLS_ROOT!s}. The package "
        "expects a `skills/` directory at the repository root next to `src/`, "
        "containing the `log-investigation` skill (a directory with a SKILL.md)."
    )

# `SkillsMiddleware` sources are directories that *contain* skill directories,
# not individual skills: with `FilesystemBackend(root_dir=SKILLS_ROOT)` the
# source `"/"` scans SKILLS_ROOT and loads `/log-investigation/SKILL.md`.
# Passing `"/log-investigation"` would make the scan list the SKILL.md file
# itself (not a directory) and load zero skills — verified against
# deepagents 0.7.23 — so the source stays the skills root.
SKILLS_SOURCES = ["/"]

# Filesystem/execution tools are irrelevant to a log-investigation agent and only
# distract it. `read_file` is deliberately NOT excluded: the agent reads the
# `log-investigation` skill with it (the `FilesystemBackend` rooted at
# SKILLS_ROOT exposes the skill text through `read_file`).
_EXCLUDED_TOOLS = frozenset(
    {
        "write_file",
        "edit_file",
        "delete",
        "execute",
        "glob",
        "grep",
        "ls",
    }
)


_profile_lock = threading.Lock()
_profile_registered = False


def _ensure_profile_registered() -> None:
    """Register the deepseek harness restriction exactly once.

    Disables the auto-added general-purpose subagent — the agent calls the tools
    directly, so no ``task`` tool is exposed — and strips the filesystem
    write/edit/execute/search tools so the agent is limited to thinking, reading
    skills, calling the database tools, and finishing with a summary.

    This mutates *process-global* deepseek harness-profile state, so it is
    guarded by a double-checked lock and is idempotent: the registration only
    needs to happen once, before the first agent is constructed.
    """
    global _profile_registered
    if _profile_registered:
        return
    with _profile_lock:
        if _profile_registered:
            return
        register_harness_profile(
            "deepseek",
            HarnessProfileConfig(
                general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
                excluded_tools=_EXCLUDED_TOOLS,
            ),
        )
        _profile_registered = True


@functools.lru_cache(maxsize=None)
def get_find_evidence_agent(effort: str) -> CompiledStateGraph:
    """Evidence agent configured for an effort level.

    ``lru_cache`` makes the per-effort cache concurrency-safe and idempotent; use
    :func:`clear_find_evidence_agent_cache` to drop it (tests / plugin reload).
    """
    _ensure_profile_registered()
    spec = get_effort_spec(effort)
    tools, mcp_enabled = get_db_tools()
    agent_kwargs = {
        "model": deepseek,
        "tools": tools,
        "middleware": [
            log_tool_calls,
            handle_tool_errors,
            TodoListMiddleware(),
            ToolCallLimitMiddleware(run_limit=spec.max_tool_calls),
            ToolRetryMiddleware(
                max_retries=MAX_TOOL_RETRIES,
                on_failure="error",
                max_delay=5,
            ),
            make_error_middleware(),
        ],
        "system_prompt": FIND_EVIDENCE_SYSTEM_PROMPT + (
            "\nAvailable MCP skill tools: call list_skills, then read_skill(name) "
            "before applying a relevant skill."
            if mcp_enabled else ""
        ),
    }
    if not mcp_enabled:
        agent_kwargs["backend"] = FilesystemBackend(root_dir=SKILLS_ROOT, virtual_mode=True)
        agent_kwargs["skills"] = SKILLS_SOURCES

    agent = create_deep_agent(**agent_kwargs)
    return agent


def clear_find_evidence_agent_cache() -> None:
    """Drop the cached per-effort agents (tests / plugin reload)."""
    get_find_evidence_agent.cache_clear()
