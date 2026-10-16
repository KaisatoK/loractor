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
from backend.pipeline.contracts import EvidenceReport, Hypothesis
from backend.pipeline.prompts import EVIDENCE_REPORT_SYSTEM_PROMPT
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
_REPORT_JSON = json.dumps({"summary": "s", "evidence": [], "hypotheses": []})


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


# --- evidence_report.generate_evidence_report -----------------------------


def test_generate_evidence_report_uses_system_prompt_and_json_example(monkeypatch):
    recorder = _RecordingModel()
    monkeypatch.setattr(evidence_report, "deepseek", recorder)

    report = evidence_report.generate_evidence_report(
        request="count the errors",
        role="engineer: investigate a specific error",
        investigation="We ran search_logs_tool and found 3 errors.",
        results=_RECORDS,
    )

    # The model's text is parsed into the report contract and returned.
    assert report == EvidenceReport(summary="s", evidence=[], hypotheses=[])

    messages = recorder.messages
    # The system prompt is the system message, and it keeps its rules
    # (grounding, exact tool names/params, unavailable marking).
    assert isinstance(messages[0], SystemMessage)
    assert messages[0].content == EVIDENCE_REPORT_SYSTEM_PROMPT
    assert "Preserve tool names and parameters exactly" in messages[0].content

    # DeepSeek json mode needs the literal word "json" plus a JSON example; both
    # may come from the system prompt or the user message.
    combined = "".join(m.content for m in messages)
    assert "json" in combined.lower()
    assert '"summary"' in combined and '"evidence"' in combined

    # The user message carries the request/role/investigation/catalog; the
    # catalog is built from the id-carrying captured records.
    user = messages[1]
    assert isinstance(user, HumanMessage)
    assert "count the errors" in user.content
    assert "engineer: investigate a specific error" in user.content
    assert "We ran search_logs_tool and found 3 errors." in user.content
    assert "R1: search_logs_tool" in user.content


def test_generate_evidence_report_spec_enumerates_tool_status_values(monkeypatch):
    # The contract's ToolStatus is the closed set {"success", "failure"}, but the
    # shape example only ever shows "success". A model that emits "failed" for a
    # failed tool call is rejected by pydantic with a literal_error, so the spec
    # must enumerate both allowed values explicitly. Pin it here so a future trim
    # cannot silently drop the enumeration again.
    recorder = _RecordingModel()
    monkeypatch.setattr(evidence_report, "deepseek", recorder)

    evidence_report.generate_evidence_report(
        request="count the errors",
        role="engineer: investigate a specific error",
        investigation="We ran search_logs_tool and found 3 errors.",
        results=_RECORDS,
    )

    spec = evidence_report.JSON_OUTPUT_SPEC
    assert "tool_used[].status" in spec
    assert '"success"' in spec
    assert '"failure"' in spec
    assert '"failed"' in spec  # named and ruled out, not silently tolerated

    # The enumeration must reach the model through the user message.
    user = recorder.messages[1]
    assert '"success"' in user.content and '"failure"' in user.content


def test_generate_evidence_report_propagates_model_errors(monkeypatch):
    # There is no empty-report fallback: a model failure escapes the entry
    # point so the caller sees it.
    monkeypatch.setattr(evidence_report, "deepseek", _RaisingModel())

    with pytest.raises(RuntimeError, match="model boom"):
        evidence_report.generate_evidence_report(
            request="count the errors",
            role="engineer: investigate a specific error",
            investigation="We ran search_logs_tool and found 3 errors.",
            results=_RECORDS,
        )


def test_generate_evidence_report_streams_without_response_format(monkeypatch):
    # The streamed reasoning projects incrementally only when the call carries no
    # structured-output guarantee; ``response_format`` (in any form) collapses it
    # to a single delta, so the live progress panel would show one block. Pin its
    # absence here so it cannot be reintroduced silently.
    recorder = _RecordingModel()
    monkeypatch.setattr(evidence_report, "deepseek", recorder)

    report = evidence_report.generate_evidence_report(
        request="count the errors",
        role="engineer: investigate a specific error",
        investigation="We ran search_logs_tool and found 3 errors.",
        results=_RECORDS,
    )

    # The reasoning block is excluded from ``message.text``, so only the json
    # text block is parsed into the report.
    assert report == EvidenceReport(summary="s", evidence=[], hypotheses=[])
    assert recorder.kwargs == {}
    assert "response_format" not in recorder.kwargs


def test_generate_evidence_report_malformed_text_raises_validation_error(monkeypatch):
    # No fence handling or repair: text that is not valid json for the contract
    # raises rather than being swallowed.
    recorder = _RecordingModel(message=AIMessage(content="not json at all"))
    monkeypatch.setattr(evidence_report, "deepseek", recorder)

    with pytest.raises(ValidationError):
        evidence_report.generate_evidence_report(
            request="count the errors",
            role="engineer: investigate a specific error",
            investigation="We ran search_logs_tool and found 3 errors.",
            results=_RECORDS,
        )


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
    monkeypatch.setattr(graph, "generate_evidence_report", _raising_generate)

    state = {
        "effort": "Low",
        "role": {"name": "engineer", "objective": "investigate a specific error"},
        "normalized": {"question": "count the errors"},
        "messages": [],
    }

    with pytest.raises(RuntimeError, match="report boom"):
        graph.find_evidence_node(state)


# --- compiled agent (state boundary) -------------------------------------


def test_compiled_agent_output_schema_has_no_persisted_report():
    # The report is built off-agent by the formatter, so it is never a state
    # channel. Assert the exact property set so an added report field is caught
    # here.
    agent = get_find_evidence_agent("Low")
    properties = agent.get_output_jsonschema().get("properties", {})
    assert set(properties) == {
        "messages",
        "structured_response",
        "todos",
    }
