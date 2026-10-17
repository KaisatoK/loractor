"""Offline tests for the ``find_evidence`` deep agent's core pieces.

Everything here is offline: the model is never called, the DB is never touched,
and every assertion runs against pure helpers (``collect_tool_results``, the
evidence-report message builder) or the ``find_evidence`` node with a stubbed
agent and report entry point. The shared ``tests/conftest.py`` already installs
dummy provider keys and an in-memory checkpointer *before* the backend is
imported; importing ``graph`` additionally needs a ``TYPESAFE_API_KEY`` to exist,
so a dummy value is set at the top of this module before that import.
"""

import json
import os

os.environ.setdefault("TYPESAFE_API_KEY", "test-key")

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import ValidationError

from backend.pipeline import graph
from backend.pipeline.agents import evidence_report
from backend.pipeline.agents.find_evidence import get_find_evidence_agent
from backend.pipeline.contracts import EvidenceJudgement, EvidenceReport, Hypothesis
from backend.pipeline.evidence import build_evidence
from backend.pipeline.prompts import EVIDENCE_REPORT_SYSTEM_PROMPT
from backend.pipeline.tools.registry import get_db_tools
from backend.pipeline.tools.results import MAX_RESULT_ROWS, collect_tool_results


# --- helpers --------------------------------------------------------------


def _record(*, result_id, tool_name, arguments=None, rows=None):
    rows = rows if rows is not None else [{"a": 1}]
    return {
        "id": result_id,
        "tool_name": tool_name,
        "arguments": arguments if arguments is not None else {"q": "x"},
        "row_count": len(rows),
        "rows": rows,
    }


_RECORDS = [
    _record(
        result_id="R1",
        tool_name="search_logs_tool",
        arguments={"query_text": "x"},
        rows=[{"a": 1}],
    ),
    _record(
        result_id="R2",
        tool_name="count_by_category_tool",
        arguments={"type": "service"},
        rows=[{"b": 1}, {"b": 2}],
    ),
]


class _FakeMessage:
    """Minimal stand-in for a langchain message, enough for the collector."""

    def __init__(self, *, type, content=None, tool_calls=None, tool_call_id=None):
        self.type = type
        self.content = content
        self.tool_calls = tool_calls
        self.tool_call_id = tool_call_id


def _ai(*tool_calls):
    return _FakeMessage(type="ai", tool_calls=list(tool_calls))


def _call(call_id, name, **args):
    return {"id": call_id, "name": name, "args": args}


def _tool(call_id, payload):
    return _FakeMessage(type="tool", tool_call_id=call_id, content=json.dumps(payload))


class _RaisingModel:
    """A model stand-in whose ``invoke`` always raises."""

    def invoke(self, messages, **kwargs):
        raise RuntimeError("model boom")


# A minimal payload that satisfies the ``EvidenceReport`` contract; the model
# returns it as its text block and the entry point parses it locally.
_REPORT_JSON = json.dumps({"summary": "s", "hypotheses": []})


class _RecordingModel:
    """Captures each call and returns a fabricated ``AIMessage``.

    ``messages`` and ``kwargs`` record how the entry point called the model, so
    the tests can assert both the prompt it built and the absence of a
    ``response_format`` (which would collapse the streamed projection to one
    delta). The returned message carries a reasoning block and a text block by
    default, mirroring a real DeepSeek reply.
    """

    def __init__(self, message=None):
        self.message = message or AIMessage(
            content=[
                {"type": "reasoning", "reasoning": "weighing the evidence"},
                {"type": "text", "text": _REPORT_JSON},
            ]
        )
        self.messages = None
        self.kwargs = None

    def invoke(self, messages, **kwargs):
        self.messages = messages
        self.kwargs = kwargs
        return self.message


# --- collect_tool_results -------------------------------------------------


def test_collect_tool_results_numbers_in_result_order():
    messages = [
        _ai(
            _call("c1", "search_logs_tool", query_text="x"),
            _call("c2", "count_by_category_tool", type="service"),
        ),
        _tool("c1", {"status": "success", "data": [{"a": 1}, {"a": 2}]}),
        _tool("c2", {"status": "success", "data": [{"b": 1}]}),
    ]

    records = collect_tool_results(messages)

    assert [r["id"] for r in records] == ["R1", "R2"]
    assert [r["tool_name"] for r in records] == [
        "search_logs_tool",
        "count_by_category_tool",
    ]
    assert [r["row_count"] for r in records] == [2, 1]
    assert records[0]["arguments"] == {"query_text": "x"}
    assert records[0]["rows"] == [{"a": 1}, {"a": 2}]


def test_collect_tool_results_skips_failed_and_empty_calls():
    messages = [
        _ai(
            _call("c1", "search_logs_tool"),
            _call("c2", "get_trend_tool"),
            _call("c3", "get_data_tool"),
        ),
        _tool("c1", {"status": "failure", "data": [{"a": 1}]}),
        _tool("c2", {"status": "success", "data": []}),
        _tool("c3", {"status": "success", "data": [{"a": 1}]}),
    ]

    records = collect_tool_results(messages)

    assert [r["id"] for r in records] == ["R1"]
    assert [r["tool_name"] for r in records] == ["get_data_tool"]


def test_collect_tool_results_caps_rows_but_reports_true_count():
    messages = [
        _ai(_call("c1", "get_data_tool")),
        _tool(
            "c1",
            {"status": "success", "data": [{"i": i} for i in range(MAX_RESULT_ROWS + 10)]},
        ),
    ]

    records = collect_tool_results(messages)

    assert records[0]["row_count"] == MAX_RESULT_ROWS + 10
    assert len(records[0]["rows"]) == MAX_RESULT_ROWS


# --- evidence_report.generate_evidence_judgement --------------------------


def _evidence():
    calls = [
        {"call_id": "c1", "tool_name": "search_logs_tool",
         "arguments": {"query_text": "x"}, "status": "success",
         "error_message": None, "result_ids": ["R1"]},
        {"call_id": "c2", "tool_name": "get_trend_tool",
         "arguments": {"type": "service"}, "status": "success",
         "error_message": None, "result_ids": []},
        {"call_id": "c3", "tool_name": "count_by_category_tool",
         "arguments": {"type": "pid"}, "status": "failure",
         "error_message": "db down", "result_ids": []},
    ]
    return build_evidence(calls, _RECORDS)


def _judge(**overrides):
    kwargs = dict(
        request="count the errors",
        role="engineer: investigate a specific error",
        investigation="We ran search_logs_tool and found 3 errors.",
        evidence=_evidence(),
        results=_RECORDS,
    )
    kwargs.update(overrides)
    return evidence_report.generate_evidence_judgement(**kwargs)


def test_generate_evidence_judgement_uses_system_prompt_and_json_example(monkeypatch):
    recorder = _RecordingModel()
    monkeypatch.setattr(evidence_report, "deepseek", recorder)

    judgement = _judge()

    # The model's text is parsed into the judgement contract and returned.
    assert judgement == EvidenceJudgement(summary="s", hypotheses=[])

    messages = recorder.messages
    assert isinstance(messages[0], SystemMessage)
    assert messages[0].content == EVIDENCE_REPORT_SYSTEM_PROMPT

    # DeepSeek json mode needs the literal word "json" plus a JSON example.
    combined = "".join(m.content for m in messages)
    assert "json" in combined.lower()
    assert '"summary"' in combined and '"hypotheses"' in combined

    user = messages[1]
    assert isinstance(user, HumanMessage)
    assert "count the errors" in user.content
    assert "engineer: investigate a specific error" in user.content
    assert "We ran search_logs_tool and found 3 errors." in user.content


def test_prompt_lists_the_code_built_evidence_items(monkeypatch):
    recorder = _RecordingModel()
    monkeypatch.setattr(evidence_report, "deepseek", recorder)
    _judge()
    user = recorder.messages[1].content
    assert "- E1: search_logs_tool({'query_text': 'x'}) -> R1 [unknown, 1 rows]" in user
    assert "- E2: get_trend_tool({'type': 'service'}) -> no rows" in user
    assert "- E3: count_by_category_tool({'type': 'pid'}) -> failed: db down" in user
    assert "captured results catalog" not in user


def test_spec_no_longer_asks_the_model_for_code_built_fields():
    spec = evidence_report.JSON_OUTPUT_SPEC
    for field in ("tool_used", "availability", "result_ids", '"evidence":'):
        assert field not in spec
    # Closed vocabularies and the cap stay in the prompt text.
    assert '"supported"' in spec and '"unsupported"' in spec and '"inconclusive"' in spec
    assert "Cite ONLY the `E`-ids listed" in spec


def test_json_spec_states_the_hypothesis_cap():
    # The contract rejects more than 3 hypotheses, but no schema is transmitted,
    # so the prompt text must say so (a live run produced 4 and failed validation).
    assert "AT MOST 3" in evidence_report.JSON_OUTPUT_SPEC


def test_generate_evidence_judgement_propagates_model_errors(monkeypatch):
    monkeypatch.setattr(evidence_report, "deepseek", _RaisingModel())
    with pytest.raises(RuntimeError, match="model boom"):
        _judge()


def test_generate_evidence_judgement_streams_without_response_format(monkeypatch):
    # ``response_format`` (in any form) collapses the streamed reply to one
    # delta, so the live progress panel would show one block. Pin its absence.
    recorder = _RecordingModel()
    monkeypatch.setattr(evidence_report, "deepseek", recorder)
    assert _judge() == EvidenceJudgement(summary="s", hypotheses=[])
    assert recorder.kwargs == {}
    assert "response_format" not in recorder.kwargs


def test_generate_evidence_judgement_malformed_text_raises_validation_error(monkeypatch):
    recorder = _RecordingModel(message=AIMessage(content="not json at all"))
    monkeypatch.setattr(evidence_report, "deepseek", recorder)
    with pytest.raises(ValidationError):
        _judge()


def test_judgement_with_four_hypotheses_is_rejected():
    hypothesis = {
        "hypothesis": "h", "description": "d", "status": "inconclusive", "evidence_ids": [],
    }
    with pytest.raises(ValidationError):
        EvidenceJudgement.model_validate({"summary": "s", "hypotheses": [hypothesis] * 4})


# --- EvidenceReport contract ----------------------------------------------


def test_hypothesis_evidence_ids_may_cite_unknown_ids():
    # The referential rule ("Each id must refer to an entry in the report's
    # evidence list") is prompt guidance only. Nothing on the contract enforces
    # it, so a report whose hypothesis cites an id absent from ``evidence`` must
    # validate, and the cited ids must survive verbatim for prompt interpolation.
    report = EvidenceReport(
        summary="s",
        evidence=[],
        hypotheses=[
            Hypothesis(
                hypothesis="Loader saturation explains the errors.",
                description="The error rate tracks request volume.",
                status="inconclusive",
                evidence_ids=["E1", "E404"],
            )
        ],
    )

    assert report.hypotheses[0].evidence_ids == ["E1", "E404"]


# --- find_evidence_node ---------------------------------------------------


class _StubInvestigationAgent:
    def invoke(self, payload):
        return {"messages": []}


def _raising_generate(**_kwargs):
    raise RuntimeError("report boom")


def test_find_evidence_node_propagates_report_failure(monkeypatch):
    # The node no longer owns a fallback: a report-generation failure must
    # propagate out of the node rather than be swallowed into an empty report.
    monkeypatch.setattr(
        graph, "get_find_evidence_agent", lambda effort: _StubInvestigationAgent()
    )
    monkeypatch.setattr(graph, "generate_evidence_judgement", _raising_generate)

    state = {
        "effort": "Low",
        "role": {"name": "engineer", "objective": "investigate a specific error"},
        "normalized": {"question": "count the errors"},
        "messages": [],
    }

    with pytest.raises(RuntimeError, match="report boom"):
        graph.find_evidence_node(state)


# --- compiled agent (state boundary) -------------------------------------


def test_compiled_agent_never_persists_the_report():
    """The invariant, in either transport: the report is not a state channel.

    It is built off-agent by the formatter from the agent's free-text summary,
    so a report field appearing here would mean the agent had started carrying
    its own conclusions across the state boundary.
    """
    properties = set(
        get_find_evidence_agent("Low").get_output_jsonschema().get("properties", {})
    )
    assert properties >= {"messages", "structured_response", "todos"}
    assert not {p for p in properties if "report" in p or "evidence" in p}


def test_compiled_agent_state_channels_follow_the_skill_backend():
    """The exact channel set, which depends on where skill text is read from.

    Local transport hands the agent a disk-backed ``FilesystemBackend`` rooted
    at ``skills/``, so skill files are on disk and absent from the state. Under
    MCP the skill text arrives through the ``list_skills`` / ``read_skill``
    tools instead, no backend is passed, and deepagents installs its default
    store -- which keeps files *in graph state* and so adds a ``files``
    channel.

    That channel is unused in MCP mode, but it is deepagents' default rather
    than something this code asks for, so it is asserted rather than fought.
    The discriminator is ``mcp_enabled``, not ``TOOLS_TRANSPORT``:
    ``TOOLS_TRANSPORT=mcp`` falls back to local tools when the service is
    unreachable, and the agent is built from the same value.
    """
    _tools, mcp_enabled = get_db_tools()
    expected = {"messages", "structured_response", "todos"}
    if mcp_enabled:
        expected.add("files")

    properties = set(
        get_find_evidence_agent("Low").get_output_jsonschema().get("properties", {})
    )
    assert properties == expected, (
        f"unexpected state channels with mcp_enabled={mcp_enabled}: "
        f"{sorted(properties ^ expected)}"
    )
