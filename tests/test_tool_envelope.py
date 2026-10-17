"""Offline tests for the tool-result contract (``tools/envelope.py``).

Covers truncation logic, executed-query capture through ``query_runner``, the
local/MCP envelope merge, and ``collect_tool_results`` with both a full and a
legacy envelope. No database or model is touched.
"""

import json
from contextlib import contextmanager

from backend.db import capture, connection
from backend.db.capture import capture_queries, record_query
from backend.pipeline.tools.envelope import (
    TOOL_KINDS,
    build_envelope,
    is_truncated,
    run_enveloped,
)
from backend.pipeline.tools.executor import execute_database_tool
from backend.pipeline.tools.results import collect_tool_results
from backend.pipeline.tools import __all__ as TOOL_NAMES


class _Msg:
    def __init__(self, *, type, content=None, tool_calls=None, tool_call_id=None):
        self.type = type
        self.content = content
        self.tool_calls = tool_calls
        self.tool_call_id = tool_call_id


class _Runtime:
    def __init__(self):
        self.events = []
        self.stream_writer = self.events.append


# --- truncation -------------------------------------------------------------


def test_truncated_when_total_exceeds_returned():
    assert is_truncated(10, 10, 37) is True
    assert is_truncated(5, 10, 5) is False


def test_truncated_from_limit_when_total_unknown():
    assert is_truncated(10, 10, None) is True  # hit the cap: more may exist
    assert is_truncated(3, 10, None) is False  # below the cap: complete


def test_truncation_unknown_without_limit_or_total():
    assert is_truncated(4, None, None) is None


def test_build_envelope_drops_none_filters_and_labels_dataset():
    env = build_envelope(
        operation="op",
        data=[{"a": 1}],
        kind="search",
        dataset="openstack",
        filters={"query_text": "x", "category": None},
        limit=10,
        queries=["SELECT 1"],
    )
    assert env["filters"] == {"query_text": "x"}
    assert env["dataset"] == "openstack"
    assert env["query"] == "SELECT 1"
    assert env["returned"] == 1 and env["truncated"] is False


# --- query capture ----------------------------------------------------------


def test_capture_only_records_inside_block():
    record_query("SELECT outside")
    with capture_queries() as captured:
        record_query(b"  SELECT 1\n\n   FROM log  ")
    record_query("SELECT after")
    assert captured == ["SELECT 1\nFROM log"]
    assert capture._captured.get() is None


def test_query_runner_records_bound_statement(monkeypatch):
    class _Cursor:
        query = None

        def execute(self, statement, params=None):
            if params is not None:
                self.query = b"SELECT * FROM log WHERE line_id = 7"

        def fetchall(self):
            return [{"line_id": 7}]

    @contextmanager
    def fake_cursor():
        yield _Cursor()

    monkeypatch.setattr(connection, "db_cursor", fake_cursor)

    @connection.query_runner
    def lookup():
        return "SELECT * FROM log WHERE line_id = %s", [7]

    with capture_queries() as captured:
        assert lookup() == [{"line_id": 7}]
    assert captured == ["SELECT * FROM log WHERE line_id = 7"]


# --- executor / MCP merge ---------------------------------------------------


def test_executor_returns_envelope_with_captured_query():
    runtime = _Runtime()

    def function():
        record_query("SELECT service, COUNT(*) FROM log GROUP BY service")
        return [{"service": "nova", "count": 3}]

    env = execute_database_tool(
        runtime=runtime,
        operation="rank",
        function=function,
        kind="aggregate",
        dataset="openstack",
        filters={"group_by": "service"},
        limit=5,
    )
    assert env["status"] == "success"
    assert env["kind"] == "aggregate"
    assert env["query"].startswith("SELECT service")
    assert env["truncated"] is False
    assert [e["status"] for e in runtime.events] == ["started", "completed"]


def test_remote_envelope_metadata_wins():
    remote = {
        "status": "success",
        "operation": "remote op",
        "kind": "context",
        "dataset": "bgl",
        "query": "SELECT remote",
        "filters": {"line_id": 5, "window": 10},
        "limit": 21,
        "total_matches": 40,
        "data": [{"line_id": i} for i in range(21)],
    }
    env = run_enveloped(
        operation="local op", function=lambda: remote, kind="search", dataset="openstack"
    )
    assert env["kind"] == "context"
    assert env["dataset"] == "bgl"
    assert env["query"] == "SELECT remote"
    assert env["filters"] == {"line_id": 5, "window": 10}
    assert env["truncated"] is True and env["total_matches"] == 40


def test_remote_legacy_payload_is_wrapped_as_rows():
    legacy = {"status": "success", "operation": "x", "data": [{"a": 1}]}
    env = run_enveloped(operation="x", function=lambda: legacy, kind="aggregate")
    assert env["data"] == [{"a": 1}]
    assert env["kind"] == "aggregate"
    assert env["query"] is None and env["truncated"] is None


# --- collection -------------------------------------------------------------


def _transcript(*payloads):
    calls = [{"id": f"c{i}", "name": name, "args": {}} for i, (name, _) in enumerate(payloads)]
    messages = [_Msg(type="ai", tool_calls=calls)]
    for i, (_, payload) in enumerate(payloads):
        messages.append(_Msg(type="tool", tool_call_id=f"c{i}", content=json.dumps(payload)))
    return messages


def test_collect_keeps_contract_fields_from_full_envelope():
    env = build_envelope(
        operation="search",
        data=[{"line_id": 1}],
        kind="search",
        dataset="openstack",
        filters={"query_text": "timeout"},
        limit=1,
        queries=["SELECT ..."],
    )
    (record,) = collect_tool_results(_transcript(("search_logs_tool", env)))
    assert record["kind"] == "search"
    assert record["dataset"] == "openstack"
    assert record["query"] == "SELECT ..."
    assert record["filters"] == {"query_text": "timeout"}
    assert record["truncated"] is True
    assert record["rows_capped"] is False


def test_collect_legacy_envelope_fills_unknown_with_none():
    legacy = {"status": "success", "data": [{"a": 1}]}
    (record,) = collect_tool_results(
        _transcript(("count_by_category_tool", legacy), )
    )
    assert record["kind"] == "aggregate"  # inferred from the tool name only
    for field in ("dataset", "query", "filters", "limit", "truncated", "total_matches"):
        assert record[field] is None


def test_every_registered_tool_has_a_kind():
    assert set(TOOL_NAMES) <= set(TOOL_KINDS)


def test_uncapped_queries_are_complete_not_unknown():
    # Grouped counts, trends, catalogs and key lookups have no LIMIT: every
    # matching group is returned, so ``truncated`` is False rather than unknown.
    assert is_truncated(12, None, None) is None
    assert is_truncated(12, None, None, complete=True) is False
    env = build_envelope(operation="o", data=[{"a": 1}], kind="aggregate", complete=True)
    assert env["truncated"] is False
    # A row cap still wins over ``complete``.
    assert is_truncated(5, 5, None, complete=True) is True


def test_rows_are_json_safe_so_langchain_keeps_them_parseable():
    # Regression: Decimal/datetime rows (search results) made LangChain fall back
    # to a Python repr, so ``collect_tool_results`` silently dropped the result.
    import datetime as dt
    from decimal import Decimal

    from langchain_core.messages import ToolCall
    from langchain_core.tools import tool

    row = {"log_timestamp": dt.datetime(2017, 5, 16, 0, 0, 20, 345000),
           "relevance": Decimal("0.0327868852"), "when": dt.date(2017, 5, 16)}
    env = build_envelope(operation="o", data=[row], kind="search")
    assert env["data"] == [{"log_timestamp": "2017-05-16 00:00:20.345000",
                            "relevance": 0.0327868852, "when": "2017-05-16"}]

    @tool
    def search() -> dict:
        """x"""
        return env

    message = search.invoke(ToolCall(name="search", args={}, id="c1", type="tool_call"))
    records = collect_tool_results([
        _Msg(type="ai", tool_calls=[{"id": "c1", "name": "search_logs_tool", "args": {}}]),
        _Msg(type="tool", tool_call_id="c1", content=message.content),
    ])
    assert [r["kind"] for r in records] == ["search"]
    assert records[0]["rows"][0]["relevance"] == 0.0327868852


# --- total_matches from the query layer (T1) ----------------------------------


def test_a_result_carrying_total_matches_makes_truncated_exact():
    from backend.db.stats import QueryResult

    rows = [{"n": i} for i in range(10)]
    more = run_enveloped(operation="op", kind="search", limit=10,
                         function=lambda: QueryResult(rows, total_matches=194))
    assert more["total_matches"] == 194
    assert more["truncated"] is True

    # Exactly the limit, and nothing more: the cap was hit but nothing was cut.
    exact = run_enveloped(operation="op", kind="search", limit=10,
                          function=lambda: QueryResult(rows, total_matches=10))
    assert exact["total_matches"] == 10
    assert exact["truncated"] is False


def test_a_result_without_total_matches_keeps_the_limit_heuristic():
    from backend.db.stats import QueryResult

    rows = [{"n": i} for i in range(10)]
    env = run_enveloped(operation="op", kind="search", limit=10,
                        function=lambda: QueryResult(rows))
    assert env["total_matches"] is None
    assert env["truncated"] is True
