"""Offline tests for role objectives, role playbooks and their prompt wiring."""

import os

os.environ.setdefault("TYPESAFE_API_KEY", "test-key")

import asyncio

from fastmcp import Client

from backend.mcp_server import server
from backend.pipeline import playbooks
from backend.pipeline.prompts.builders import PipelinePrompts
from backend.pipeline.roles import ROLE_OBJECTIVES, ROLE_SKILLS


def _state(role):
    return {
        "role": {"name": role, "objective": ROLE_OBJECTIVES[role]},
        "effort": "Medium",
        "normalized": {"normalized_question": "What recurs?"},
    }


def test_every_role_has_objective_and_playbook():
    assert set(ROLE_OBJECTIVES) == set(ROLE_SKILLS) == {"engineer", "team_lead", "manager"}
    for role in ROLE_SKILLS:
        body = playbooks.role_playbook(role)
        assert body.startswith("#") and "---" not in body.splitlines()[0]
        assert "## Evidence to collect" in body


def test_objectives_follow_the_brief():
    manager = ROLE_OBJECTIVES["manager"].lower()
    assert "do not invent financial losses" in manager
    assert "revenue" not in manager.replace("do not invent financial", "")
    for objective in ROLE_OBJECTIVES.values():
        assert "never incidents" in objective  # row counts are not incidents
        assert "hypothesis" in objective


def test_find_evidence_prompt_injects_only_the_users_playbook():
    prompts = PipelinePrompts()
    engineer = prompts.find_evidence(_state("engineer"))
    lead = prompts.find_evidence(_state("team_lead"))
    assert "Engineer investigation playbook" in engineer
    assert "Recurring-patterns playbook" not in engineer
    assert "Recurring-patterns playbook" in lead
    # Both get the ownership mapping so teams can be named.
    assert "Compute Platform" in engineer and "Compute Platform" in lead


def test_team_for_maps_known_and_unknown_components():
    assert playbooks.team_for("nova.compute.manager") == "Compute Platform"
    assert playbooks.team_for("not.a.service") == "unowned"


def test_mcp_server_serves_role_playbooks():
    async def run():
        async with Client(server.mcp) as client:
            listed = await client.call_tool("list_skills", {})
            names = {item["name"] for item in listed.structured_content["result"]}
            assert set(ROLE_SKILLS.values()) <= names

    asyncio.run(run())
