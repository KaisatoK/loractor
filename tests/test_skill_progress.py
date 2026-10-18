"""Offline tests: the progress panel shows which skill an investigation used.

Two sources, labelled differently: the role playbook that ``find_evidence``
injects into the prompt, and a skill the agent reads itself (``read_file`` of
``<skill>/SKILL.md``, or ``read_skill``). The line is rendered once, in
``stream_events.progress_line``.
"""

import json
import os
from types import SimpleNamespace

os.environ.setdefault("TYPESAFE_API_KEY", "test-key")

import pytest
from langchain.messages import ToolMessage

from backend.jobs import RunRegistry, RunState
from backend.pipeline import graph
from backend.pipeline.middleware import report_skill_reads
from backend.pipeline.roles import ROLE_SKILLS
from backend.pipeline.skill_progress import (
    injected_playbook_event,
    read_skill_event,
    skill_read_by,
)
from stream_events import apply_event, new_live_state, progress_line


# --- the events and the line ---------------------------------------------------
@pytest.mark.parametrize("role", sorted(ROLE_SKILLS))
def test_the_injected_playbook_names_the_roles_skill(role):
    event = injected_playbook_event(role)
    assert event["type"] == "tool_progress" and event["status"] == "skill"
    assert progress_line(event) == f"📖 Playbook: {ROLE_SKILLS[role]} (injected)"


def test_a_read_skill_is_labelled_differently_from_the_injected_playbook():
    assert progress_line(read_skill_event("log-investigation")) == (
        "📖 Read skill: log-investigation"
    )


def test_live_and_persisted_progress_agree_on_the_skill_lines():
    events = [
        {"type": "tool_progress", "content": json.dumps(injected_playbook_event("manager"))},
        {"type": "tool_progress", "content": json.dumps(read_skill_event("log-investigation"))},
    ]
    live = new_live_state()
    run = RunState(run_id="r", user_id="u", chat_id="c")
    for event in events:
        apply_event(live, event, persist=False)
        RunRegistry._track(None, run, event)
    assert live["progress"] == run.progress == [
        "📖 Playbook: risk-prioritization (injected)",
        "📖 Read skill: log-investigation",
    ]


# --- which tool calls count as reading a skill ---------------------------------
@pytest.mark.parametrize(
    ("call", "skill"),
    [
        ({"name": "read_skill", "args": {"name": "recurring-patterns"}}, "recurring-patterns"),
        ({"name": "read_file", "args": {"file_path": "/log-investigation/SKILL.md"}}, "log-investigation"),
        ({"name": "read_file", "args": {"file_path": "log-investigation/SKILL.md"}}, "log-investigation"),
        # Not a skill being read:
        ({"name": "read_file", "args": {"file_path": "/log-investigation/notes.md"}}, None),
        ({"name": "read_file", "args": {"file_path": "/SKILL.md"}}, None),
        ({"name": "read_file", "args": {"file_path": "/a/b/SKILL.md"}}, None),
        ({"name": "read_file", "args": {}}, None),
        ({"name": "read_skill", "args": {}}, None),
        ({"name": "read_skill", "args": {"name": ""}}, None),
        ({"name": "search_logs_tool", "args": {"name": "x"}}, None),
        ({"name": "read_file"}, None),
    ],
)
def test_skill_read_by(call, skill):
    assert skill_read_by(call) == skill


# --- the middleware ------------------------------------------------------------
def _request(call, written):
    return SimpleNamespace(tool_call=call, runtime=SimpleNamespace(stream_writer=written.append))


def _wrap(call, result):
    written: list = []
    returned = report_skill_reads.wrap_tool_call(_request(call, written), lambda request: result)
    return returned, written


def test_reading_a_skill_reports_it_and_returns_the_result_untouched():
    call = {"name": "read_file", "args": {"file_path": "/log-investigation/SKILL.md"}, "id": "1"}
    result = ToolMessage(content="body", tool_call_id="1")
    returned, written = _wrap(call, result)
    assert returned is result
    assert written == [read_skill_event("log-investigation")]


def test_a_failed_read_is_not_reported():
    call = {"name": "read_skill", "args": {"name": "nope"}, "id": "1"}
    _, written = _wrap(call, ToolMessage(content="x", tool_call_id="1", status="error"))
    assert written == []


def test_other_tools_report_nothing():
    call = {"name": "count_by_dataset_tool", "args": {}, "id": "1"}
    _, written = _wrap(call, ToolMessage(content="x", tool_call_id="1"))
    assert written == []


def test_a_writer_that_fails_never_fails_the_tool_call():
    def broken(_event):
        raise RuntimeError("no stream")

    call = {"name": "read_skill", "args": {"name": "log-investigation"}, "id": "1"}
    result = ToolMessage(content="body", tool_call_id="1")
    request = SimpleNamespace(tool_call=call, runtime=SimpleNamespace(stream_writer=broken))
    assert report_skill_reads.wrap_tool_call(request, lambda r: result) is result


def _agent(tool):
    """A real agent graph, driven by a scripted model, with the two middlewares."""
    from langchain.agents import create_agent
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage

    from backend.pipeline.middleware import handle_tool_errors

    class Scripted(GenericFakeChatModel):
        def bind_tools(self, *_args, **_kwargs):
            return self

    call = AIMessage(
        content="",
        tool_calls=[{"name": tool.name, "args": {"name": "log-investigation"}, "id": "c1"}],
    )
    return create_agent(
        Scripted(messages=iter([call, AIMessage(content="done")])),
        tools=[tool],
        middleware=[handle_tool_errors, report_skill_reads],
    )


def _custom(agent):
    return [
        chunk
        for chunk in agent.stream(
            {"messages": [("user", "go")]}, stream_mode="custom"
        )
    ]


def test_a_real_agent_run_streams_the_skill_read():
    from langchain_core.tools import tool

    @tool
    def read_skill(name: str) -> str:
        """Return a skill."""
        return f"body of {name}"

    assert _custom(_agent(read_skill)) == [read_skill_event("log-investigation")]


def test_a_real_agent_run_does_not_report_a_read_that_raised():
    from langchain_core.tools import tool

    @tool
    def read_skill(name: str) -> str:
        """Fail."""
        raise RuntimeError("no such skill")

    # handle_tool_errors turns the exception into an error message first.
    assert _custom(_agent(read_skill)) == []


# --- the node ------------------------------------------------------------------
class _StubAgent:
    def invoke(self, payload):
        return {"messages": []}


def test_find_evidence_announces_the_injected_playbook_before_the_agent_runs(monkeypatch):
    order: list = []
    monkeypatch.setattr(graph, "_emit", order.append)

    class Agent:
        def invoke(self, payload):
            order.append("agent")
            return {"messages": []}

    monkeypatch.setattr(graph, "get_find_evidence_agent", lambda effort: Agent())

    def stop(**_kwargs):
        raise RuntimeError("stop here")

    monkeypatch.setattr(graph, "generate_evidence_judgement", stop)
    state = {
        "effort": "Low",
        "role": {"name": "team_lead", "objective": "find recurring problems"},
        "normalized": {"question": "q"},
        "messages": [],
    }
    with pytest.raises(RuntimeError, match="stop here"):
        graph.find_evidence_node(state)

    assert order == [injected_playbook_event("team_lead"), "agent"]
