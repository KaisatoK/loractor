"""Role prompts, resources and completions (plan M3).

The prompts must be assembled from the pipeline's own functions, and every
resource rendered from its source: the checks below fail if either starts to hold
a copy of its own.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import asdict
from pathlib import Path

import pytest
from fastmcp import Client
from mcp.types import PromptReference, ResourceTemplateReference

from backend.db.datasets import catalog, dataset_ids
from backend.mcp_server import server
from backend.mcp_server.prompts import (
    complete,
    role_prompt_text,
    scope_policy,
    worked_investigation_text,
)
from backend.pipeline import priority
from backend.pipeline.answers import (
    APPENDIX_HEADING,
    FINDING_TITLES,
    RECOMMENDATIONS_TITLE,
    answer_layout,
)
from backend.pipeline.effort import EFFORT_SPECS
from backend.pipeline.playbooks import ownership_context, role_answer_rules, role_playbook
from backend.pipeline.prompts.builders import PipelinePrompts, evidence_brief
from backend.pipeline.roles import OVERVIEW_ROLES, ROLE_OBJECTIVES, ROLE_SKILLS

SRC = Path(__file__).resolve().parents[1] / "src" / "backend"


def _run(fn, **client_kwargs):
    async def go():
        async with Client(server.mcp, **client_kwargs) as client:
            return await fn(client)

    return asyncio.run(go())


def _role(name):
    return {"name": name, "objective": ROLE_OBJECTIVES[name]}


# --- The brief is the pipeline's own ---------------------------------------------------
@pytest.mark.parametrize("role", list(ROLE_SKILLS))
@pytest.mark.parametrize("effort", [*EFFORT_SPECS, None])
def test_the_pipeline_prompt_is_the_shared_brief_plus_its_own_state(role, effort):
    """``find_evidence`` is ``evidence_brief`` followed by pipeline state, so the
    chatbot's agent and an external agent are briefed by one function. (The prompt
    was verified byte-identical to the pre-refactor text for every role/effort.)"""
    state = {"role": _role(role), "normalized": "why did nova fail?"}
    if effort is not None:
        state["effort"] = effort
    prompt = PipelinePrompts().find_evidence(state)
    assert prompt.startswith(evidence_brief(_role(role), effort if effort else "Medium"))
    assert prompt[len(evidence_brief(_role(role), effort or "Medium")):].startswith("<datasets>\n")


@pytest.mark.parametrize("role", list(ROLE_SKILLS))
def test_the_brief_carries_what_the_role_needs(role):
    brief = evidence_brief(_role(role), "High")
    assert ROLE_OBJECTIVES[role] in brief
    assert role_playbook(role) in brief
    assert ownership_context() in brief
    assert f"at most {EFFORT_SPECS['High'].max_tool_calls} tool calls" in brief


# --- Prompts ---------------------------------------------------------------------------
def test_there_is_one_prompt_per_role_and_a_worked_investigation():
    prompts = {p.name: p for p in _run(lambda c: c.list_prompts())}
    assert set(prompts) == {f"investigate_as_{r}" for r in ROLE_SKILLS} | {"worked_investigation"}
    for role, skill in ROLE_SKILLS.items():
        prompt = prompts[f"investigate_as_{role}"]
        assert prompt.description and "Use when" in prompt.description  # the skill's own words
        assert {a.name for a in prompt.arguments} == {"question", "effort"}
        required = {a.name for a in prompt.arguments if a.required}
        assert required == {"question"}


@pytest.mark.parametrize("role", list(ROLE_SKILLS))
def test_a_role_prompt_is_the_brief_the_rules_the_layout_and_the_question(role):
    async def render(client):
        return await client.get_prompt(
            f"investigate_as_{role}", {"question": "what keeps failing?", "effort": "Extreme"}
        )

    result = _run(render)
    text = result.messages[0].content.text
    assert text == role_prompt_text(role, "what keeps failing?", "Extreme")
    assert text.startswith(evidence_brief(_role(role), "Extreme"))
    assert role_answer_rules(role) in text
    assert answer_layout() in text and scope_policy(role) in text
    assert text.rstrip().endswith("what keeps failing?\n</question>")


def test_answer_layout_names_the_sections_the_chatbot_builds():
    layout = answer_layout()
    for title in (*FINDING_TITLES.values(), RECOMMENDATIONS_TITLE, APPENDIX_HEADING):
        assert title in layout


def test_scope_policy_follows_the_overview_roles():
    for role in ROLE_SKILLS:
        assert ("dataset-wide overview is a valid scope" in scope_policy(role)) == (
            role in OVERVIEW_ROLES
        )
    assert "ask the user for one" in scope_policy("engineer")


def test_the_worked_investigation_gives_every_role_its_own_evidence_plan():
    text = worked_investigation_text("nova timeouts", "Medium")
    for role in ROLE_SKILLS:
        assert f"## As {role}" in text and role_playbook(role) in text
    assert text.count(answer_layout()) == 1  # shared, not repeated per role


def test_prompts_name_only_tools_that_exist():
    served = {t.name for t in _run(lambda c: c.list_tools())}
    for role in ROLE_SKILLS:
        named = set(re.findall(r"\b\w+_tool\b", role_prompt_text(role, "q", "Low")))
        named |= set(re.findall(r"\b\w+_tool\b", scope_policy(role)))
        planned = {"get_nearby_events_tool", "count_by_team_tool"}  # in the playbooks until they land
        assert named - planned <= served, named - planned - served


# --- Completions -----------------------------------------------------------------------
def test_completions_offer_the_closed_vocabularies():
    class Arg:
        def __init__(self, name, value=""):
            self.name, self.value = name, value

    prompt = PromptReference(type="ref/prompt", name="investigate_as_manager")
    assert complete(prompt, Arg("effort"), None) == list(EFFORT_SPECS)
    assert complete(prompt, Arg("effort", "ex"), None) == ["Extreme"]
    template = ResourceTemplateReference(type="ref/resource", uri="logs://datasets/{dataset_id}/schema")
    assert complete(template, Arg("dataset_id", "o"), None) == [
        d for d in dataset_ids() if d.startswith("o")
    ]
    roles = ResourceTemplateReference(type="ref/resource", uri="logs://roles/{role}")
    assert complete(roles, Arg("role"), None) == list(ROLE_SKILLS)
    assert complete(prompt, Arg("question"), None) is None


def test_the_server_advertises_completions_and_answers_them():
    async def ask(client):
        return await client.complete(
            PromptReference(type="ref/prompt", name="investigate_as_engineer"),
            {"name": "effort", "value": "H"},
        )

    assert _run(ask, mode="legacy").values == ["High"]


# --- Resources -------------------------------------------------------------------------
def _read(uri, **kw):
    async def read(client):
        return (await client.read_resource(uri))[0].text

    return _run(read, **kw)


def test_the_skills_are_served_with_their_manifest():
    uris = {str(r.uri) for r in _run(lambda c: c.list_resources())}
    for skill in ROLE_SKILLS.values():
        assert f"skill://{skill}/SKILL.md" in uris and f"skill://{skill}/_manifest" in uris
    assert "skill://log-investigation/SKILL.md" in uris
    assert "# Log Investigation" in _read("skill://log-investigation/SKILL.md")
    manifest = json.loads(_read("skill://risk-prioritization/_manifest"))
    assert manifest["skill"] == "risk-prioritization"
    assert any(f["path"] == "SKILL.md" for f in manifest["files"])
    # the file itself is the one source: the resource and the pipeline read it
    assert role_playbook("manager") in _read("skill://risk-prioritization/SKILL.md")


def test_a_skill_path_cannot_escape_its_directory():
    async def read(client):
        return await client.read_resource("skill://risk-prioritization/../../README.md")

    with pytest.raises(Exception):
        _run(read)


@pytest.mark.parametrize("dataset_id", dataset_ids())
def test_dataset_schema_is_the_registry_summary_without_sql_names(dataset_id):
    body = json.loads(_read(f"logs://datasets/{dataset_id}/schema"))
    assert body == {"dataset": dataset_id, **catalog()[dataset_id]}
    assert body["severity_values"] and body["fields"]


def test_an_unknown_dataset_or_role_is_an_error():
    for uri in ("logs://datasets/nope/schema", "logs://roles/ceo"):
        with pytest.raises(Exception):
            _read(uri)


def test_ownership_is_the_fictional_mapping():
    assert _read("logs://ownership") == ownership_context()
    assert "Fictional" in _read("logs://ownership")


@pytest.mark.parametrize("role", list(ROLE_SKILLS))
def test_role_resource(role):
    body = json.loads(_read(f"logs://roles/{role}"))
    assert body == {
        "role": role, "objective": ROLE_OBJECTIVES[role], "skill": ROLE_SKILLS[role],
        "overview_is_a_valid_scope": role in OVERVIEW_ROLES,
    }


def test_policy_resources_render_their_sources():
    contract = json.loads(_read("logs://policy/result-contract"))
    assert contract["measured_kinds"] == ["aggregate", "trend"]
    assert contract["envelope"]["required"] == ["status"]
    score = json.loads(_read("logs://policy/priority-score"))
    assert score["formula"] == priority.FORMULA and score["weights"] == priority.WEIGHTS
    effort = json.loads(_read("logs://policy/effort"))
    assert effort == {k: asdict(v) for k, v in EFFORT_SPECS.items()}


def test_the_server_holds_no_copy_of_the_texts_it_renders():
    """A heading or rule typed into mcp_server/ would drift from the code that
    enforces it; the server may only import them."""
    forbidden = [
        *FINDING_TITLES.values(), RECOMMENDATIONS_TITLE, APPENDIX_HEADING,
        "never incidents", priority.FORMULA, *ROLE_OBJECTIVES.values(),
    ]
    for path in (SRC / "mcp_server").glob("*.py"):
        text = path.read_text()
        for needle in forbidden:
            assert needle not in text, f"{path.name} repeats {needle[:40]!r}"
