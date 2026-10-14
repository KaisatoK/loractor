"""Pin the committed projection path's capture of depth-1 tool progress.

The ``api.py`` revert restores this projection shape::

    run.interleave("messages", "values", "subgraphs", "custom")

with the ``subgraphs`` branch drilling each direct child's ``.custom``. That
scope-exact projection only sees ``tool_progress`` written at the direct child
handle's own namespace (depth 1) — which is exactly where a database tool on the
*main* agent writes, because the agent's ToolNode sits directly under the
pipeline's ``find_evidence`` node now that the separate evidence-gathering
subagent is gone.

The graph built below is shaped like the pipeline and is fully offline:
``find_evidence`` (a node) invokes a compiled agent whose ToolNode tool writes a
``tool_progress`` payload with ``get_stream_writer()`` — the same mechanism as
``pipeline/tools/executor.py``. The first test asserts the depth-1 payload is
surfaced exactly once through the committed projection while a root-scope
``custom`` read alone does not see it. The second pins the contrast the revert
depends on: a write routed through a nested ``task``-style ``.invoke()`` lands at
depth 2 and is not captured at all.
"""

from typing import Annotated, Any, TypedDict

from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from langgraph.stream.transformers import CustomTransformer


class MsgState(TypedDict):
    messages: Annotated[list, add_messages]


def _write_progress(site: str) -> None:
    get_stream_writer()(
        {
            "type": "tool_progress",
            "operation": site,
            "status": "started",
            "message": f"Starting {site}.",
        }
    )


def _call(name: str, call_id: str, **args: Any) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}],
    )


def _build_direct_graph():
    """``find_evidence`` invokes an agent whose *own* tool writes progress.

    This mirrors the new pipeline: the database tool runs on the main agent, so
    its write lands at the direct child handle's namespace (depth 1).
    """

    @tool
    def db_tool(q: str) -> str:
        """Emit progress from the agent's ToolNode, then 'return rows'."""
        _write_progress("db_tool")
        return "rows"

    agent = StateGraph(MsgState)
    agent.add_node("tools", ToolNode([db_tool]))
    agent.add_edge(START, "tools")
    agent.add_edge("tools", END)
    agent_graph = agent.compile()

    def find_evidence_node(state: MsgState) -> dict[str, Any]:
        agent_graph.invoke({"messages": [_call("db_tool", "c1", q="x")]})
        return {}

    top = StateGraph(MsgState)
    top.add_node("find_evidence", find_evidence_node)
    top.add_edge(START, "find_evidence")
    top.add_edge("find_evidence", END)
    return top.compile()


def _build_nested_graph():
    """``task``-style nesting: a tool that ``.invoke()``s a lower agent graph.

    This reproduces the old delegated-subagent path, where the DB tool's write is
    tagged with the ambient namespace of the tool node hosting the ``task`` call
    (depth 2).
    """

    @tool
    def db_tool(q: str) -> str:
        """Emit progress from inside the lower graph's ToolNode."""
        _write_progress("db_tool")
        return "rows"

    lower = StateGraph(MsgState)
    lower.add_node("tools", ToolNode([db_tool]))
    lower.add_edge(START, "tools")
    lower.add_edge("tools", END)
    lower_graph = lower.compile()

    @tool
    def task(q: str) -> str:
        """Invoke the lower graph (mirrors deepagents' task tool)."""
        lower_graph.invoke({"messages": [_call("db_tool", "c1", q=q)]})
        return "nested done"

    agent = StateGraph(MsgState)
    agent.add_node("tools", ToolNode([task]))
    agent.add_edge(START, "tools")
    agent.add_edge("tools", END)
    agent_graph = agent.compile()

    def find_evidence_node(state: MsgState) -> dict[str, Any]:
        agent_graph.invoke({"messages": [_call("task", "t1", q="go")]})
        return {}

    top = StateGraph(MsgState)
    top.add_node("find_evidence", find_evidence_node)
    top.add_edge(START, "find_evidence")
    top.add_edge("find_evidence", END)
    return top.compile()


def _drive_committed_projection(compiled):
    """Reproduce ``api._stream_events``' committed projection shape.

    Returns ``(root_custom, drilled_custom)``: the ``tool_progress`` payloads the
    root ``custom`` branch saw, and those the ``subgraphs`` → ``.custom`` drill
    saw. This is the projection the reverted ``api.py`` drives.
    """
    run = compiled.stream_events(
        {"messages": []}, version="v3", transformers=[CustomTransformer]
    )
    root: list[Any] = []
    drilled: list[Any] = []
    for name, item in run.interleave("messages", "values", "subgraphs", "custom"):
        if name == "custom":
            if isinstance(item, dict) and item.get("type") == "tool_progress":
                root.append(item)
        elif name == "subgraphs":
            for sub_name, sub_item in item.interleave("messages", "custom"):
                if (
                    sub_name == "custom"
                    and isinstance(sub_item, dict)
                    and sub_item.get("type") == "tool_progress"
                ):
                    drilled.append(sub_item)
    return root, drilled


def _root_custom_only(compiled):
    """Read the root-scope ``custom`` projection alone."""
    run = compiled.stream_events(
        {"messages": []}, version="v3", transformers=[CustomTransformer]
    )
    return [
        item
        for name, item in run.interleave("custom")
        if isinstance(item, dict) and item.get("type") == "tool_progress"
    ]


def test_committed_projection_captures_depth_one_tool_progress():
    compiled = _build_direct_graph()

    root, drilled = _drive_committed_projection(compiled)

    # Surfaced exactly once, through the subgraph handle's `.custom` drill.
    assert [p["operation"] for p in drilled] == ["db_tool"]
    assert drilled[0]["type"] == "tool_progress"
    assert drilled[0]["status"] == "started"

    # The root-scope `custom` branch never sees a write emitted at the child
    # handle's namespace — neither in the full projection nor on its own.
    assert root == []
    assert _root_custom_only(compiled) == []


def test_nested_task_depth_two_progress_is_not_captured():
    compiled = _build_nested_graph()

    root, drilled = _drive_committed_projection(compiled)

    # Depth-2 writes are invisible to the scope-exact committed projection —
    # which is why the api.py revert had to land with the tool move.
    assert root == []
    assert drilled == []
