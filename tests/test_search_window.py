"""Offline tests for time-filtered search with an exact match count (T1).

Builders are reached through ``__wrapped__`` so no database is touched. Live
checks are in ``test_openstack_live.py`` and ``test_bgl_live.py``.

Background, measured before the change (OpenStack, 'instance' at ERROR): the
search ranked the top 100 rows by BM25 and only then filtered by severity, so it
returned 0 of the 24 real matches. Severity and the window now apply inside the
ranked branch.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.pipeline.tools.schemas import SearchLogs

LO = "2017-05-16 00:00:00"
HI = "2017-05-16 06:00:00"


# --- Schema -----------------------------------------------------------------------
def test_the_window_is_optional():
    args = SearchLogs(query_text="timeout")
    assert args.start_time is None and args.end_time is None


def test_both_bounds_are_accepted():
    args = SearchLogs(query_text="timeout", start_time=LO, end_time=HI)
    assert (args.start_time, args.end_time) == (LO, HI)


@pytest.mark.parametrize(
    "window",
    [
        {"start_time": LO},                        # one bound alone
        {"end_time": HI},
        {"start_time": HI, "end_time": LO},        # inverted
        {"start_time": LO, "end_time": LO},        # empty
        {"start_time": "yesterday", "end_time": HI},  # not a timestamp
    ],
)
def test_a_bad_window_is_rejected(window):
    with pytest.raises(ValidationError):
        SearchLogs(query_text="timeout", **window)


# --- SQL shape: filters inside the ranked branch (Task 4) -------------------------
from backend.db import query, templates  # noqa: E402
from backend.db.errors import QueryRefused  # noqa: E402
from sql_render import normalized  # noqa: E402


@pytest.fixture
def lexical(monkeypatch):
    """No template vectors: OpenStack and BGL search lexically, as today."""
    monkeypatch.setattr(templates, "has_template_vectors", lambda _d: False)


def _search(*args, **kwargs):
    return query.search_logs.__wrapped__(*args, **kwargs)


def _branch(text: str) -> str:
    """The ranked branch: everything before its row cap."""
    return text.split("LIMIT 100")[0]


def test_no_filter_keeps_the_branch_on_the_index_alone(lexical):
    text, params = (normalized(_search("timeout", dataset="openstack")[0]),
                    _search("timeout", dataset="openstack")[1])
    branch = _branch(text)
    assert 'FROM "openstack_idx" "i" WHERE "i"."Content" @@ ts_any(' in branch
    assert "JOIN" not in branch
    assert params == ["timeout"]


def test_severity_is_matched_in_the_index_before_the_cut(lexical):
    query_sql, params, _ = _search("instance", dataset="openstack", severity="ERROR")
    text = normalized(query_sql)
    branch = _branch(text)
    # Severity is a verbatim index column: matched there, no join needed.
    assert 'AND "i"."Level" @@ %s' in branch
    assert "JOIN" not in branch
    # ...and repeated on the joined row as a guard.
    assert 'WHERE "t"."Level" = %s' in text
    assert params == ["instance", "ERROR", "ERROR"]


def test_a_window_joins_the_base_table_inside_the_branch(lexical):
    query_sql, params, stats = _search(
        "instance", dataset="openstack", start_time=LO, end_time=HI
    )
    text = normalized(query_sql)
    branch = _branch(text)
    assert 'JOIN "openstack" "bt" ON "bt"."LineId" = "i"."LineId"' in branch
    assert (
        '"bt"."Timestamp" >= %s::timestamptz AND "bt"."Timestamp" < %s::timestamptz'
        in branch
    )
    assert 'BM25("i".tableoid)' in branch
    # The guard on the outer join.
    assert '"t"."Timestamp" >= %s::timestamptz AND "t"."Timestamp" < %s::timestamptz' in text
    assert params == ["instance", LO, HI, LO, HI]
    assert stats.window_seconds == 6 * 3600


def test_severity_and_window_together_keep_their_parameter_order(lexical):
    _, params, _ = _search(
        "error", dataset="bgl", severity="FATAL", start_time=LO, end_time=HI
    )
    # Branch: terms, severity, window; then the outer guard: severity, window.
    assert params == ["error", "FATAL", LO, HI, "FATAL", LO, HI]


def test_a_half_window_is_refused_by_the_query_layer(lexical):
    with pytest.raises(QueryRefused) as refused:
        _search("timeout", dataset="openstack", start_time=LO)
    assert refused.value.refusal.code == "half_window"


def test_the_dataset_window_limit_still_applies(lexical):
    with pytest.raises(QueryRefused) as refused:
        _search("timeout", dataset="openstack",
                start_time="2017-01-01 00:00:00", end_time="2017-12-01 00:00:00")
    assert refused.value.refusal.code == "window_too_wide"


def test_a_wrong_case_severity_is_still_refused(lexical):
    with pytest.raises(QueryRefused) as refused:
        _search("timeout", dataset="openstack", severity="error")
    assert refused.value.refusal.code == "severity_case"


# --- The count (Task 5) -----------------------------------------------------------
def _count(*args, **kwargs):
    return query.count_search_matches.__wrapped__(*args, **kwargs)


def test_the_count_without_a_window_reads_the_index_only():
    query_sql, params, _ = _count("instance", dataset="openstack", severity="ERROR")
    text = normalized(query_sql)
    assert text.startswith('SELECT count(*) AS n FROM "openstack_idx" "i" WHERE')
    assert 'AND "i"."Level" @@ %s' in text
    assert "JOIN" not in text and "LIMIT" not in text
    assert params == ["instance", "ERROR"]


def test_the_count_with_a_window_joins_like_the_branch():
    query_sql, params, _ = _count(
        "instance", dataset="openstack", start_time=LO, end_time=HI
    )
    text = normalized(query_sql)
    assert 'JOIN "openstack" "bt" ON "bt"."LineId" = "i"."LineId"' in text
    assert "LIMIT" not in text
    assert params == ["instance", LO, HI]


def test_search_with_total_attaches_the_count(monkeypatch, lexical):
    from backend.db.stats import QueryResult

    calls = []
    monkeypatch.setattr(query, "search_logs",
                        lambda *a: calls.append(("search", a)) or QueryResult([{"r": 1}] * 10))
    monkeypatch.setattr(query, "count_search_matches",
                        lambda *a: calls.append(("count", a)) or QueryResult([{"n": 194}]))
    result = query.search_with_total("instance", "openstack", "ERROR", 10, LO, HI)

    assert result.total_matches == 194
    assert calls == [
        ("search", ("instance", "openstack", "ERROR", 10, LO, HI)),
        ("count", ("instance", "openstack", "ERROR", LO, HI)),
    ]


def test_a_refused_search_is_not_counted(monkeypatch, lexical):
    from backend.db.errors import Refusal
    from backend.db.stats import QueryResult

    refused = QueryResult([], refusal=Refusal(reason="r", suggestion="s", code="c"))
    monkeypatch.setattr(query, "search_logs", lambda *a: refused)
    monkeypatch.setattr(query, "count_search_matches",
                        lambda *a: pytest.fail("a refused search must not be counted"))
    assert query.search_with_total("x", "openstack").total_matches is None


# --- The in-place index (Task 6, offline only) ------------------------------------
def test_the_index_only_search_filters_window_and_severity_on_the_index():
    query_sql, params, _ = _search(
        "charge", dataset="otel_slice", severity="error",
        start_time="2025-09-23 00:00:00+00", end_time="2025-09-23 00:10:00+00",
    )
    text = normalized(query_sql)
    assert 'FROM "otel_ext_idx" WHERE "Body" @@ ts_any(' in text
    assert 'AND "SeverityText" @@ %s' in text
    assert '"Timestamp" >= %s::timestamptz AND "Timestamp" < %s::timestamptz' in text
    assert "JOIN" not in text
    assert params == ["charge", "error", "2025-09-23 00:00:00+00", "2025-09-23 00:10:00+00"]


def test_the_index_only_count_uses_the_same_conditions():
    query_sql, params, _ = _count(
        "charge", dataset="otel_slice", severity="error",
        start_time="2025-09-23 00:00:00+00", end_time="2025-09-23 00:10:00+00",
    )
    text = normalized(query_sql)
    assert text.startswith('SELECT count(*) AS n FROM "otel_ext_idx" WHERE "Body" @@')
    assert params == ["charge", "error", "2025-09-23 00:00:00+00", "2025-09-23 00:10:00+00"]


# --- Semantic branches (Task 7) ---------------------------------------------------
def test_the_template_branch_is_filtered_too(monkeypatch):
    monkeypatch.setattr(templates, "has_template_vectors", lambda _d: True)
    query_sql, params, _ = _search(
        "memory fault", dataset="bgl", severity="FATAL", start_time=LO, end_time=HI
    )
    text = normalized(query_sql)
    template_part = text.split("UNION ALL")[1].split("SELECT \"t\".")[0]
    assert '"r"."Level" @@ %s' in template_part
    assert 'JOIN "bgl" "bt" ON "bt"."LineId" = "r"."LineId"' in template_part
    assert '"bt"."Timestamp" >= %s::timestamptz' in template_part
    # ai_embed, lexical (terms, sev, window), template (sev, window), outer guard.
    assert params == ["memory fault", "memory", "fault", "FATAL", LO, HI,
                      "FATAL", LO, HI, "FATAL", LO, HI]


def test_a_semantic_search_reports_its_total_as_unknown(monkeypatch):
    from backend.db.stats import QueryResult

    monkeypatch.setattr(templates, "has_template_vectors", lambda _d: True)
    monkeypatch.setattr(query, "search_logs", lambda *a: QueryResult([{"r": 1}]))
    monkeypatch.setattr(query, "count_search_matches",
                        lambda *a: pytest.fail("semantic matches are not countable"))
    result = query.search_with_total("memory fault", "bgl")
    assert result.total_matches is None
    assert any("not counted" in n for n in result.stats.notes)


# --- The tool, invoked as the agent invokes it (Task 8) ---------------------------
@pytest.mark.parametrize("transport", ["local", "mcp"])
def test_the_tool_passes_the_window_and_reports_total_matches(monkeypatch, transport):
    import json

    from backend.db.stats import QueryResult
    from backend.pipeline.tools import mcp_tools, tools
    from tool_invoke import tool_call

    seen = {}

    def fake_search(*args):
        seen["args"] = args
        return QueryResult([{"record_id": i} for i in range(10)], total_matches=24)

    if transport == "local":
        monkeypatch.setattr(tools, "search_with_total", fake_search)
        tool = tools.search_logs_tool
    else:
        # The MCP wrapper forwards to the server; stand the server's tool in.
        from backend.mcp_server import server

        class _Bridge:
            def call_tool(self, name, arguments):
                assert name == "search_logs_tool"
                # The server function behind @mcp.tool, called in-process.
                return server.search_logs_tool.fn(**arguments)

        monkeypatch.setattr(server, "search_with_total", fake_search)
        monkeypatch.setattr(mcp_tools, "get_mcp_bridge", lambda: _Bridge())
        tool = mcp_tools.search_logs_tool

    events: list = []
    message = tool.invoke(tool_call(
        tool, events, query_text="instance", category="ERROR",
        dataset="openstack", start_time=LO, end_time=HI,
    ))
    result = json.loads(message.content)

    assert seen["args"] == ("instance", "openstack", "ERROR", 10, LO, HI)
    assert result["total_matches"] == 24
    assert result["returned"] == 10
    assert result["truncated"] is True
    assert result["filters"] == {
        "query_text": "instance", "category": "ERROR", "start_time": LO, "end_time": HI,
    }
    assert "from 2017-05-16 00:00:00 to 2017-05-16 06:00:00" in result["operation"]


def test_the_search_window_reaches_the_answer_data_scope():
    from backend.pipeline.data_scope import build_data_scope

    scope = build_data_scope([{
        "id": "R1", "kind": "search", "dataset": "openstack",
        "filters": {"query_text": "instance", "start_time": LO, "end_time": HI},
        "truncated": True,
    }])
    assert scope.time_windows == [{"start": LO, "end": HI}]
    assert scope.example_result_ids == ["R1"]


def test_the_mcp_server_rejects_a_half_window():
    import asyncio

    from fastmcp import Client

    from backend.mcp_server import server

    async def run():
        async with Client(server.mcp) as client:
            result = await client.call_tool(
                "search_logs_tool",
                {"query_text": "x", "dataset": "openstack", "start_time": LO},
                raise_on_error=False,
            )
            assert result.is_error

    asyncio.run(run())
