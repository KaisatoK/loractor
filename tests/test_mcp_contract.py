"""The MCP server's contract with a client (plan M2): guidance, result schema,
size limits and error messages."""

from __future__ import annotations

import asyncio
import json
import re
from importlib.metadata import version

import jsonschema
import pytest
from fastmcp import Client
from psycopg2 import OperationalError, errors

from backend.db.errors import refuse
from backend.db.stats import QueryResult
from backend.mcp_server import server
from backend.mcp_server.instructions import build_instructions
from backend.pipeline.roles import COMMON_ANSWER_RULES, OVERVIEW_ROLES, ROLE_SKILLS
from backend.pipeline.tools import specs
from backend.pipeline.tools.envelope import (
    CUT_MARKER,
    MAX_CELL_CHARS,
    ToolEnvelope,
    build_envelope,
    clip_rows,
    run_enveloped,
)
from backend.pipeline.tools.local import LOCAL_TOOLS


def _run(coro_fn, **client_kwargs):
    async def go():
        async with Client(server.mcp, **client_kwargs) as client:
            return await coro_fn(client)

    return asyncio.run(go())


WINDOWED = {
    "start_time": "2017-05-16 00:00:00", "end_time": "2017-05-16 01:00:00",
    "type": "service",
}


def _call(name: str, arguments: dict):
    return _run(lambda c: c.call_tool(name, arguments, raise_on_error=False))


# --- Guidance ------------------------------------------------------------------
def test_the_server_introduces_itself_with_guidance_in_both_protocol_eras():
    for mode in ("legacy", "auto"):

        async def read(client):
            return client.instructions, client.server_info

        instructions, info = _run(read, mode=mode)
        assert instructions == build_instructions()
        assert info.name == "serenedb-logs"
        assert info.version == version("ai-chatbot-test")


def test_the_instructions_are_rendered_from_the_modules_that_own_the_rules():
    text = build_instructions()
    for role, skill in ROLE_SKILLS.items():
        assert f"{role}: {skill}" in text
    assert COMMON_ANSWER_RULES.strip() in text
    assert "aggregate and trend" in text  # MEASURED_KINDS
    for role in OVERVIEW_ROLES:
        assert role in text
    assert len(text.split()) < 300  # guidance, not a manual


def test_every_tool_the_instructions_name_exists():
    named = set(re.findall(r"\b\w+_tool\b|\bread_skill\b|\blist_skills\b", build_instructions()))
    served = {t.name for t in _run(lambda c: c.list_tools())}
    assert named and named <= served, named - served


def test_the_instructions_tell_the_agent_to_rank_with_the_tool():
    """An external agent has no answer_node, so it must be pointed at the
    server-only ranking tool and the formula it applies (never its own arithmetic)."""
    from backend.pipeline import priority

    text = build_instructions()
    assert "rank_priorities_tool" in text
    assert priority.FORMULA in text


# --- The result schema -----------------------------------------------------------
def test_every_evidence_tool_advertises_the_shared_envelope_as_its_output():
    tools = {t.name: t for t in _run(lambda c: c.list_tools())}
    for spec in specs.SPECS:
        if spec.available:
            schema = tools[spec.name].output_schema
            assert schema["required"] == ["status"]
            assert {"data", "query", "filters", "truncated", "total_matches"} <= set(
                schema["properties"]
            )
            assert all(p.get("description") for p in schema["properties"].values())


def test_a_real_success_and_a_real_refusal_both_validate_against_the_schema():
    schema = ToolEnvelope.model_json_schema()
    success = build_envelope(
        operation="op", data=[{"service": "nova", "count": 3}], kind="aggregate",
        dataset="openstack", filters={"start_time": "2017-05-16 00:00:00"}, limit=5,
        queries=["SELECT 1"], query_stats={"rows_scanned": 3, "notes": []},
    )
    jsonschema.validate(success, schema)

    def refused():
        raise refuse("window too wide", "narrow to 300 s", code="window")

    refusal = run_enveloped(operation="op", function=refused, kind="aggregate")
    assert refusal["status"] == "refused"
    jsonschema.validate(refusal, schema)


def test_a_refused_query_is_a_result_not_an_error(monkeypatch):
    def refused(*args):
        raise refuse("would scan 340M rows", "narrow to 300 s")

    monkeypatch.setattr(specs, "get_field_values", refused)
    result = _call("get_data_tool", WINDOWED)
    assert not result.is_error
    assert result.structured_content["status"] == "refused"
    assert result.structured_content["suggestion"] == "narrow to 300 s"


# --- Size limits -----------------------------------------------------------------
def test_long_text_is_cut_with_a_visible_marker_and_counted():
    rows, clipped, dropped = clip_rows([{"message": "x" * (MAX_CELL_CHARS + 50), "n": 1}])
    assert rows[0]["message"] == "x" * MAX_CELL_CHARS + CUT_MARKER
    assert (clipped, dropped) == (1, 0)
    # ... also inside nested cells such as the sweep's trace
    rows, clipped, _ = clip_rows([{"trace": [{"note": "y" * 3000}]}])
    assert rows[0]["trace"][0]["note"].endswith(CUT_MARKER) and clipped == 1


def test_a_result_over_the_budget_loses_trailing_rows_and_says_so():
    data = [{"i": i, "message": "z" * 1_900} for i in range(150)]
    envelope = build_envelope(
        operation="op", data=data, kind="search", limit=50, queries=["q"],
    )
    # Not a count, so returning fewer rows than were found must read as truncated.
    assert envelope["returned"] == len(envelope["data"]) < 150
    assert envelope["truncated"] is True
    assert envelope["clipped_rows"] == 150 - envelope["returned"]
    # A single oversized row is kept: a result is never emptied.
    assert len(clip_rows([{"m": "a" * 10}], max_total=1)[0]) == 1


def test_a_complete_aggregate_is_not_clipped_but_a_lookup_never_is():
    big = "q" * (MAX_CELL_CHARS * 2)
    lookup = build_envelope(operation="op", data=[{"message": big}], kind="lookup", complete=True)
    assert lookup["data"][0]["message"] == big and "clipped_cells" not in lookup
    search = build_envelope(operation="op", data=[{"message": big}], kind="search", limit=10)
    assert search["data"][0]["message"].endswith(CUT_MARKER) and search["clipped_cells"] == 1


def test_both_transports_return_the_same_clipped_rows(monkeypatch):
    from tool_invoke import tool_call

    long_row = {"record_id": 1, "message": "m" * 5000}
    monkeypatch.setattr(
        specs, "search_with_total",
        lambda *a: QueryResult([long_row], total_matches=1),
    )
    events: list = []
    tool = LOCAL_TOOLS["search_logs_tool"]
    local = json.loads(tool.invoke(tool_call(tool, events, query_text="m")).content)
    served = _call("search_logs_tool", {"query_text": "m"}).structured_content
    assert local["data"] == served["data"]
    assert local["data"][0]["message"].endswith(CUT_MARKER)
    assert local["clipped_cells"] == served["clipped_cells"] == 1


def test_the_pipeline_wrapper_keeps_the_servers_clipping_record():
    remote = {
        "status": "success", "operation": "op", "kind": "search", "data": [{"a": 1}],
        "clipped_cells": 3, "clipped_rows": 2, "truncated": True, "filters": {},
    }
    envelope = run_enveloped(operation="op", function=lambda: remote, kind="search")
    assert envelope["clipped_cells"] == 3 and envelope["clipped_rows"] == 2


# --- Errors ----------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raised", "must_say", "must_not_say"),
    [
        (errors.QueryCanceled("canceling statement due to statement timeout"),
         "Narrow the time window", "statement timeout"),
        (OperationalError("connection to server at 10.0.0.9 failed"),
         "could not reach the database", "10.0.0.9"),
        (RuntimeError("password=hunter2 in /srv/secret.py"),
         "failed unexpectedly", "hunter2"),
    ],
)
def test_failures_become_actionable_messages_without_leaking_details(
    monkeypatch, raised, must_say, must_not_say
):
    def boom(*args):
        raise raised

    monkeypatch.setattr(specs, "get_field_values", boom)
    result = _call("get_data_tool", WINDOWED)
    assert result.is_error
    text = result.content[0].text
    assert must_say in text and must_not_say not in text
    assert "get_data_tool" in text
