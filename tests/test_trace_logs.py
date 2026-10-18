"""``get_trace_logs_tool``: one trace's log lines across services (server-only, O2)."""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastmcp import Client
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).parent))

from sql_render import normalized  # noqa: E402

from backend.db import pruning, trace_query  # noqa: E402
from backend.db.errors import QueryRefused  # noqa: E402
from backend.db.stats import QueryResult, QueryStats  # noqa: E402
from backend.mcp_server import server  # noqa: E402
from backend.pipeline.tools import specs  # noqa: E402
from backend.pipeline.tools.envelope import TOOL_KINDS  # noqa: E402
from backend.pipeline.tools.local import LOCAL_TOOLS  # noqa: E402
from backend.pipeline.tools.mcp_tools import MCP_TOOLS  # noqa: E402
from backend.pipeline.tools.schemas import GetTraceLogs  # noqa: E402

TRACE = "279342fae1497aa878c707290543eab1"
WINDOW = {"start_time": "2025-09-23 00:00:10+00", "end_time": "2025-09-23 00:00:30+00"}


def args(**over):
    return {"trace_id": TRACE, **WINDOW, **over}


@pytest.fixture
def no_footers(monkeypatch):
    """The scan guard reads Parquet footers; stand a small plan in for them."""
    monkeypatch.setattr(
        trace_query, "plan_and_guard",
        lambda ds, window: QueryStats(dataset=ds.id, window_seconds=window.seconds),
    )


def build(**kw):
    return trace_query.trace_logs.__wrapped__(**kw)


# --- Schema ------------------------------------------------------------------------
def test_defaults_and_required_arguments():
    parsed = GetTraceLogs(**args())
    assert parsed.dataset == "otel_s3" and parsed.limit == 100
    for bad in (
        {"trace_id": "not-hex", **WINDOW},
        {"trace_id": TRACE.upper(), **WINDOW},
        {"trace_id": TRACE[:-1], **WINDOW},
        {"trace_id": TRACE},  # the window is mandatory
        args(limit=0),
        args(limit=201),
        args(dataset="openstack"),
        args(end_time="2025-09-23 00:00:05+00"),
    ):
        with pytest.raises(ValidationError):
            GetTraceLogs(**bad)


# --- SQL ---------------------------------------------------------------------------
def test_sql_shape_filters_by_trace_inside_the_window_oldest_first(no_footers):
    query, params, _ = build(trace_id=TRACE, **WINDOW)
    text = normalized(query)
    assert 'FROM read_parquet(' in text
    assert '"TraceId" = %s' in text and '"Timestamp" >= %s::timestamptz' in text
    assert 'ORDER BY "Timestamp"' in text and "LIMIT 100" in text
    assert '"SpanId" AS span_id' in text
    # The nanosecond timestamp is truncated so the driver can parse it.
    assert "date_trunc('microsecond'" in text
    # The trace id and the bounds are parameters, never part of the SQL text.
    assert TRACE not in text and TRACE in params


def test_a_malicious_trace_id_never_reaches_the_sql_text(no_footers):
    query, params, _ = build(trace_id="x'; DROP TABLE t; --", **WINDOW)
    assert "DROP" not in normalized(query)
    assert "x'; DROP TABLE t; --" in params


# --- Refusals ----------------------------------------------------------------------
@pytest.mark.parametrize("dataset", ["openstack", "bgl", "otel_slice"])
def test_datasets_without_trace_lines_are_refused_with_a_suggestion(dataset, no_footers):
    with pytest.raises(QueryRefused) as raised:
        build(trace_id=TRACE, dataset=dataset, **WINDOW)
    assert "otel_s3" in raised.value.refusal.suggestion


def test_a_window_over_the_remote_ceiling_is_refused():
    with pytest.raises(QueryRefused) as raised:
        build(trace_id=TRACE, start_time="2025-09-23 00:00:00+00",
              end_time="2025-09-23 00:10:00+00")
    assert "300" in raised.value.refusal.suggestion


def test_limit_outside_bounds_is_refused(no_footers):
    with pytest.raises(QueryRefused):
        build(trace_id=TRACE, limit=0, **WINDOW)
    with pytest.raises(QueryRefused):
        build(trace_id=TRACE, limit=trace_query.MAX_TRACE_LINES + 1, **WINDOW)


# --- Post-processing ---------------------------------------------------------------
def _rows(*pairs):
    base = datetime(2025, 9, 23, 0, 0, 14, tzinfo=timezone.utc)
    return [
        {"ts": base + timedelta(seconds=s), "severity": sev, "service": svc,
         "span_id": f"s{i}", "message": f"m{i}"}
        for i, (s, svc, sev) in enumerate(pairs)
    ]


def test_each_line_gets_seconds_since_the_first_and_a_service_note(monkeypatch):
    rows = _rows((0, "frontend", "info"), (0.5, "payment", "error"), (7.25, "frontend", "error"))
    monkeypatch.setattr(trace_query, "trace_logs",
                        lambda *a, **k: QueryResult(rows, stats=QueryStats(dataset="otel_s3")))
    result = trace_query.get_trace_logs(TRACE, **WINDOW)
    assert [r["t_plus_s"] for r in result] == [0.0, 0.5, 7.25]
    note = " ".join(result.stats.notes)
    assert "3 line(s)" in note and "frontend, payment" in note
    assert "Only lines that carry a trace id appear" in note


def test_an_empty_trace_says_why_instead_of_implying_nothing_happened(monkeypatch):
    monkeypatch.setattr(trace_query, "trace_logs",
                        lambda *a, **k: QueryResult([], stats=QueryStats(dataset="otel_s3")))
    result = trace_query.get_trace_logs(TRACE, **WINDOW)
    assert len(result) == 0
    assert "No line in the window carries trace id" in result.stats.notes[0]


def test_hitting_the_limit_is_noted(monkeypatch):
    rows = _rows((0, "a", "info"), (1, "b", "info"))
    monkeypatch.setattr(trace_query, "trace_logs",
                        lambda *a, **k: QueryResult(rows, stats=QueryStats(dataset="otel_s3")))
    result = trace_query.get_trace_logs(TRACE, limit=2, **WINDOW)
    assert any("stopped at limit=2" in n for n in result.stats.notes)


def test_a_refusal_passes_through_untouched(monkeypatch):
    refused = QueryResult([], stats=QueryStats(), refusal=object())
    monkeypatch.setattr(trace_query, "trace_logs", lambda *a, **k: refused)
    assert trace_query.get_trace_logs(TRACE, **WINDOW) is refused


# --- The tool ----------------------------------------------------------------------
def test_the_tool_is_server_only_and_a_context_result():
    assert "get_trace_logs_tool" in {s.name for s in specs.server_specs()}
    assert "get_trace_logs_tool" not in LOCAL_TOOLS
    assert "get_trace_logs_tool" not in MCP_TOOLS
    assert "get_trace_logs_tool" not in {s.name for s in specs.pipeline_specs()}
    assert TOOL_KINDS["get_trace_logs_tool"] == "context"


def test_the_server_returns_the_shared_envelope(monkeypatch):
    rows = _rows((0, "frontend", "info"), (5, "payment", "error"))
    monkeypatch.setattr(
        specs, "get_trace_logs",
        lambda *a, **k: trace_query.QueryResult(
            [dict(r, t_plus_s=r["ts"].timestamp() - rows[0]["ts"].timestamp()) for r in rows],
            stats=QueryStats(dataset="otel_s3", notes=["n"]),
        ),
    )

    async def go():
        async with Client(server.mcp) as client:
            tools = {t.name: t for t in await client.list_tools()}
            call = await client.call_tool("get_trace_logs_tool", args(), raise_on_error=False)
            return tools["get_trace_logs_tool"], call

    tool, call = asyncio.run(go())
    assert not call.is_error
    body = call.structured_content
    assert body["status"] == "success" and body["kind"] == "context"
    assert body["dataset"] == "otel_s3" and body["returned"] == 2
    assert [r["service"] for r in body["data"]] == ["frontend", "payment"]
    assert body["filters"]["trace_id"] == TRACE
    assert set(tool.input_schema["required"]) == {"trace_id", "start_time", "end_time"}
    assert tool.annotations.read_only_hint is True


def test_a_bad_id_is_rejected_with_a_message_that_says_what_to_fix():
    async def go():
        async with Client(server.mcp) as client:
            return await client.call_tool(
                "get_trace_logs_tool", args(trace_id="nope"), raise_on_error=False
            )

    result = asyncio.run(go())
    assert result.is_error
    assert "trace_id" in result.content[0].text
