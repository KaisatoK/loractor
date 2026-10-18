"""The spec table is the single source of every evidence tool (plan M1).

A tool used to be written three times (local, MCP wrapper, MCP server) plus a
hand-written classifier-catalog line. These tests fail if any of them can drift
again: each transport is generated from ``specs.SPECS`` and must agree with it.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest
from fastmcp import Client

from backend.db.datasets import dataset_ids
from backend.mcp_server import server
from backend.mcp_server.tools import input_schema
from backend.pipeline.prompts.builders import TOOL_CATALOG
from backend.pipeline.tools import specs
from backend.pipeline.tools.envelope import TOOL_KINDS
from backend.pipeline.tools.local import LOCAL_TOOLS
from backend.pipeline.tools.mcp_tools import MCP_TOOLS

AVAILABLE = [s for s in specs.SPECS if s.available]
# The pipeline's own tools (what the local and wrapper transports generate) and
# every tool the server offers (which adds server-only ones).
PIPELINE = [s for s in specs.SPECS if s.available and s.pipeline]
SERVER = [s for s in specs.SPECS if s.available]
SRC = Path(__file__).resolve().parents[1] / "src" / "backend"


def _server_tools() -> dict:
    async def run():
        async with Client(server.mcp) as client:
            return {t.name: t for t in await client.list_tools()}

    return asyncio.run(run())


def test_every_pipeline_tool_exists_on_every_transport():
    names = {s.name for s in PIPELINE}
    assert set(LOCAL_TOOLS) == set(MCP_TOOLS) == names
    served = set(_server_tools())
    assert names <= served
    server_only = {s.name for s in SERVER if not s.pipeline}
    assert served - names == {"list_skills", "read_skill"} | server_only


def test_a_server_only_spec_is_offered_on_the_server_only(monkeypatch):
    """A ``pipeline=False`` tool reaches the server and nothing in the pipeline."""
    from dataclasses import replace

    fake = replace(
        specs.BY_NAME["count_by_dataset_tool"], name="fake_server_only_tool", pipeline=False
    )
    monkeypatch.setattr(specs, "SPECS", (*specs.SPECS, fake))
    assert "fake_server_only_tool" in {s.name for s in specs.server_specs()}
    assert "fake_server_only_tool" not in {s.name for s in specs.agent_specs()}
    assert "fake_server_only_tool" not in {s.name for s in specs.pipeline_specs()}


def test_the_rank_priorities_tool_is_server_only():
    """No answer_node exists on an external agent, so only the server ranks."""
    assert "rank_priorities_tool" in {s.name for s in specs.server_specs()}
    assert "rank_priorities_tool" not in LOCAL_TOOLS
    assert "rank_priorities_tool" not in MCP_TOOLS
    assert "rank_priorities_tool" not in TOOL_CATALOG
    assert "rank_priorities_tool" in _server_tools()


def test_planned_tools_are_declared_but_offered_nowhere():
    # None are planned now (T2/T3 landed); the rule still holds for the next one.
    planned = {s.name for s in specs.SPECS if not s.available}
    assert all(s.run is not None for s in AVAILABLE)
    assert not planned & (set(LOCAL_TOOLS) | set(MCP_TOOLS) | set(_server_tools()))
    # ... but the classifiers' catalog and the result kinds know them.
    assert planned <= set(TOOL_CATALOG) and planned <= set(TOOL_KINDS)


def test_catalog_and_kinds_are_derived_from_the_table():
    pipeline = specs.pipeline_specs()
    assert list(TOOL_CATALOG) == [s.name for s in pipeline]
    assert TOOL_CATALOG == {s.name: s.catalog for s in pipeline}
    # The kinds cover every spec, so a server-only tool still gets classified.
    assert TOOL_KINDS == {s.name: s.kind for s in specs.SPECS}


@pytest.mark.parametrize("spec", SERVER, ids=lambda s: s.name)
def test_one_description_everywhere(spec):
    served = _server_tools()[spec.name]
    assert served.description == spec.description
    assert served.title == spec.title
    if spec.pipeline:
        assert LOCAL_TOOLS[spec.name].description == spec.description
        assert MCP_TOOLS[spec.name].description == spec.description


@pytest.mark.parametrize("spec", PIPELINE, ids=lambda s: s.name)
def test_the_same_arguments_everywhere(spec):
    served = _server_tools()[spec.name].input_schema
    local = LOCAL_TOOLS[spec.name].tool_call_schema.model_json_schema()
    wrapper = MCP_TOOLS[spec.name].tool_call_schema.model_json_schema()
    assert set(served["properties"]) == set(local["properties"]) == set(wrapper["properties"])
    assert set(served.get("required", [])) == set(local.get("required", []))
    # What the server advertises is the argument model's own schema, so a bound
    # written once in schemas.py is the bound every client sees.
    for name, prop in served["properties"].items():
        for key in ("minimum", "maximum", "enum", "pattern", "maxLength", "minLength"):
            assert prop.get(key) == local["properties"][name].get(key), (name, key)
    assert served["additionalProperties"] is False


@pytest.mark.parametrize("spec", SERVER, ids=lambda s: s.name)
def test_every_argument_is_described_and_every_limit_is_bounded(spec):
    schema = input_schema(spec)
    for name, prop in schema["properties"].items():
        assert prop.get("description"), f"{spec.name}.{name} has no description"
        if name == "limit":
            assert "minimum" in prop and "maximum" in prop, f"{spec.name}.limit unbounded"
        if name == "dataset":
            assert prop.get("enum"), f"{spec.name}.dataset has no vocabulary"


def test_dataset_vocabulary_comes_from_the_registry():
    prop = input_schema(specs.BY_NAME["search_logs_tool"])["properties"]["dataset"]
    assert prop["enum"] == list(dataset_ids())
    sweep = input_schema(specs.BY_NAME["sweep_chunks_tool"])["properties"]["dataset"]
    assert set(sweep["enum"]) <= set(dataset_ids()) and sweep["default"] == "otel_s3"


@pytest.mark.parametrize("spec", SERVER, ids=lambda s: s.name)
def test_descriptions_do_not_restate_the_arguments(spec):
    """Inputs are described once, in the parameter schema; the description says
    what the tool is for, what it returns and what an empty result means."""
    assert not re.search(r"(^|\n|\. )Inputs?:", spec.description), spec.name
    assert "Returns" in spec.description, f"{spec.name} does not say what it returns"
    assert spec.kind in spec.description or spec.name == "list_datasets_tool"


def test_annotations_say_what_each_tool_does_to_the_world():
    tools = _server_tools()
    for spec in SERVER:
        ann = tools[spec.name].annotations
        assert ann.read_only_hint is spec.read_only
        assert ann.idempotent_hint is True
        assert ann.open_world_hint is spec.open_world
    # The tools that build or drop an index on disk; clients should ask before
    # running them. `otel_index_tool` joined the sweep pair when the in-place
    # OTel index became buildable on request.
    #
    # `search_logs_tool` stays read-only even though a search on otel_slice can
    # build that index: the hint is per tool, not per call, and flagging every
    # search -- including the millisecond local ones that build nothing -- would
    # misdescribe the common case. Its description carries the warning instead.
    assert {s.name for s in AVAILABLE if not s.read_only} == {
        "sweep_chunks_tool", "investigate_chunk_tool", "otel_index_tool",
    }
    assert tools["count_by_dataset_tool"].annotations.open_world_hint is False


def test_no_tool_is_written_out_by_hand_any_more():
    """The three hand-written copies are gone: apart from the two skill tools,
    nothing under ``tools/`` or ``mcp_server/`` declares a tool by decorator."""
    offenders = []
    for path in [*(SRC / "pipeline" / "tools").glob("*.py"), *(SRC / "mcp_server").glob("*.py")]:
        for match in re.finditer(r"^@(?:mcp\.)?tool\b.*\n(?:.*\n)?\s*def (\w+)", path.read_text(), re.M):
            if match.group(1) not in {"read_skill", "list_skills"}:
                offenders.append(f"{path.name}:{match.group(1)}")
    assert not offenders, offenders


def test_the_agent_gets_the_sweep_pair_last():
    order = [s.name for s in specs.agent_specs()]
    assert order[0] == "list_datasets_tool"
    assert order[-2:] == ["sweep_chunks_tool", "investigate_chunk_tool"]


# --- The server validates what the schema advertises ---------------------------
def _call(name: str, arguments: dict):
    async def run():
        async with Client(server.mcp) as client:
            return await client.call_tool(name, arguments, raise_on_error=False)

    return asyncio.run(run())


def test_the_server_rejects_what_the_schema_forbids_and_says_how_to_fix_it():
    result = _call("search_logs_tool", {"query_text": "x", "limit": 0})
    assert result.is_error
    assert "limit" in result.content[0].text
    result = _call("search_logs_tool", {"query_text": "x", "dataset": "nope"})
    assert result.is_error and "dataset" in result.content[0].text


def test_the_server_rejects_an_inverted_window_on_a_windowed_tool():
    # ``_Windowed._ordered`` owns the rule; the same bound must hold on the
    # server, not only in the pipeline's schema.
    result = _call(
        "get_trend_tool",
        {
            "start_time": "2017-05-16 01:00:00",
            "end_time": "2017-05-16 00:00:00",
            "type": ["service", "nova"],
        },
    )
    assert result.is_error
    text = result.content[0].text
    assert "end_time" in text and "start_time" in text


def test_the_server_rejects_unknown_arguments_instead_of_dropping_them():
    result = _call("search_logs_tool", {"query_text": "x", "severity": "ERROR"})
    assert result.is_error
    text = result.content[0].text
    assert "severity" in text and "category" in text  # names the accepted ones


def test_the_server_returns_the_envelope_as_structured_content(monkeypatch):
    from backend.db.stats import QueryResult

    monkeypatch.setattr(
        specs, "search_with_total",
        lambda *a: QueryResult([{"record_id": 1, "message": "boom"}], total_matches=7),
    )
    result = _call("search_logs_tool", {"query_text": "boom", "dataset": "openstack"})
    assert not result.is_error
    envelope = result.structured_content
    assert envelope["status"] == "success" and envelope["kind"] == "search"
    assert envelope["total_matches"] == 7 and envelope["truncated"] is True
    assert envelope["filters"]["query_text"] == "boom"
    assert json.loads(result.content[0].text) == envelope
