"""The ``find_evidence`` deep agent: a single deepseek agent reads the
``log-investigation`` skill, calls the nine DB tools (seven query tools plus the
two sweep tools), and ends with a free-text
summary; ``graph.py`` + ``evidence_report.py`` build the report off-agent.
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

# Tools that examine the whole corpus. Named here rather than imported from the
# tool modules so this gate does not depend on the transport in use.
SWEEP_TOOL_NAMES = frozenset({"sweep_chunks_tool", "investigate_chunk_tool"})

MAX_TOOL_RETRIES = 2

# The repo's skills directory, from this module's location. Rooting the backend
# here (not the repo root) keeps `read_file` to skills only — never `.env` or
# other files. A missing directory fails loudly instead of loading zero skills.
SKILLS_ROOT = Path(__file__).resolve().parents[4] / "skills"
if not SKILLS_ROOT.is_dir():
    raise RuntimeError(
        f"The skills directory was not found at {SKILLS_ROOT!s}. The package "
        "expects a `skills/` directory at the repository root next to `src/`, "
        "containing the `log-investigation` skill (a directory with a SKILL.md)."
    )

# `SkillsMiddleware` sources are dirs that *contain* skill dirs, not skills: with
# `FilesystemBackend(root_dir=SKILLS_ROOT)` source `"/"` scans SKILLS_ROOT and
# loads `/log-investigation/SKILL.md` (the skill path would load zero; 0.7.23).
SKILLS_SOURCES = ["/"]

# Filesystem/execution tools only distract a log-investigation agent. `read_file`
# is deliberately NOT excluded: the agent reads the `log-investigation` skill
# with it (the `FilesystemBackend` rooted at SKILLS_ROOT exposes the skill text).
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
    """Register the deepseek harness restriction exactly once (double-checked
    lock; mutates process-global state). Disables the general-purpose subagent
    (no ``task`` tool) and strips filesystem write/edit/execute/search tools."""
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
    """Evidence agent configured for an effort level. ``lru_cache`` makes the
    per-effort cache concurrency-safe and idempotent; drop it via
    :func:`clear_find_evidence_agent_cache` (tests/reload)."""
    _ensure_profile_registered()
    spec = get_effort_spec(effort)
    tools, mcp_enabled = get_db_tools()

    # The sweep is withheld below Extreme rather than offered and refused.
    #
    # A tool the model can see, it will eventually try, and a refusal still
    # costs a call out of a budget of four to twenty. More to the point, a
    # sweep's cheapest useful configuration takes minutes, which is not a thing
    # to spend on a question a bounded query answers. `max_promotions` is the
    # existing per-effort dial, so it decides this too.
    if not spec.max_promotions:
        tools = [t for t in tools if t.name not in SWEEP_TOOL_NAMES]
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
