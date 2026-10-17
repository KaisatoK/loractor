"""The OTel in-place index: coverage, automatic indexing, and the index tool.

Offline. The contract is deliberately small: a search on ``otel_slice`` indexes
the window it was given when the current index does not cover it, with no effort
gate and no cost confirmation, so such a search cannot fail for a missing index.
These assert the coverage arithmetic, that an already-covered window costs
nothing, and that the one unanswerable case (no window and no index) is a
refusal carrying the fix.
"""

from __future__ import annotations

import contextlib
import datetime as dt

import pytest

from backend.db import external
from backend.db.errors import QueryRefused
from backend.pipeline.tools import otel_index_tool
from backend.pipeline.tools.envelope import MEASURED_KINDS, TOOL_KINDS

WINDOW = ("2025-09-23 00:00:00+00", "2025-09-23 00:01:00+00")
# A window the planner will actually accept: a minute spans 55 row groups and
# is refused as too long to build, which is the planner doing its job.
PLAN_WINDOW = ("2025-09-23 00:00:07+00", "2025-09-23 00:00:08+00")


def _invoke(name: str, **args) -> tuple[dict, list]:
    """Invoke a generated tool the way the agent's ToolNode does.

    Not ``tool.func(...)``: that skips langchain's argument parsing, so defaults
    are never filled in -- and ``tests/tool_invoke.py`` exists because a tool
    that dropped its injected runtime still passed tests invoked that way.
    """
    import json

    from backend.pipeline.tools.local import LOCAL_TOOLS
    from tool_invoke import tool_call

    tool = LOCAL_TOOLS[name]
    events: list = []
    message = tool.invoke(tool_call(tool, events, **args))
    return json.loads(message.content), events


def _search(**args) -> tuple[dict, list]:
    return _invoke("search_logs_tool", **args)


def _row(result: dict) -> dict:
    assert result["status"] == "success", result
    assert result["returned"] == 1, result
    return result["data"][0]


def _coverage(monkeypatch, **overrides):
    """Install a fake index coverage report."""
    record = {
        "index": external.INDEX_NAME,
        "exists": True,
        "covers": False,
        "window_start": WINDOW[0],
        "window_end": WINDOW[1],
        "severities": ["error", "fatal"],
        "index_bytes": 36 * 1024 * 1024,
        **overrides,
    }
    monkeypatch.setattr(external, "index_coverage", lambda *a, **k: dict(record))
    return record


def _never_builds(monkeypatch):
    monkeypatch.setattr(
        external, "build_external_index",
        lambda *a, **k: pytest.fail("a covered window must not be rebuilt"),
    )


# --- Coverage arithmetic (pure) ---------------------------------------------
@pytest.mark.parametrize(
    "start, end, covered",
    [
        ("2025-09-23 00:00:10+00", "2025-09-23 00:00:20+00", True),   # inside
        ("2025-09-23 00:00:00+00", "2025-09-23 00:01:00+00", True),   # exactly
        ("2025-09-22 23:59:59+00", "2025-09-23 00:00:20+00", False),  # starts before
        ("2025-09-23 00:00:50+00", "2025-09-23 00:01:30+00", False),  # ends after
    ],
)
def test_a_window_is_covered_only_when_it_falls_inside_the_indexed_one(
    start, end, covered
):
    assert external.covers_window(start, end, *WINDOW) is covered


def test_a_naive_bound_is_read_as_utc_so_it_still_compares():
    """Sessions are pinned to UTC and the build record keeps the text it was
    given, so a naive bound must not raise against an aware one."""
    assert external.covers_window(
        "2025-09-23 00:00:10", "2025-09-23 00:00:20", *WINDOW
    ) is True
    assert external._as_utc("2025-09-23 00:00:10").tzinfo == dt.timezone.utc


@pytest.mark.parametrize(
    "args",
    [
        (None, None, *WINDOW),                      # no window asked for
        (*WINDOW, None, None),                      # nothing recorded
        ("nonsense", "also bad", *WINDOW),          # unparseable request
        (*WINDOW, "nonsense", "also bad"),          # unparseable record
    ],
)
def test_unknown_coverage_is_never_treated_as_covered(args):
    """Anything unestablished must lead to a build, not to a search of an
    index that does not hold the rows."""
    assert external.covers_window(*args) is False


def test_a_view_without_its_index_does_not_count_as_an_index(monkeypatch):
    """A build creates the view first and a drop can leave it behind, so the
    view's presence is not proof of an index -- the real state found live."""

    @contextlib.contextmanager
    def cursor():
        class Cur:
            def execute(self, statement, params=None):
                pass
            def fetchall(self):
                return [{"table_name": external.VIEW_NAME},
                        {"table_name": external.META_TABLE}]
            def fetchone(self):
                return None
        yield Cur()

    monkeypatch.setattr(external, "_cursor", cursor)
    monkeypatch.setattr(external, "_index_present", lambda cur: False)
    coverage = external.index_coverage(*WINDOW)
    assert coverage["exists"] is False and coverage["covers"] is False


# --- The search path indexes its own window ---------------------------------
def test_searching_otel_slice_indexes_an_uncovered_window_then_searches(monkeypatch):
    from backend.pipeline.tools import specs

    calls: list[str] = []
    _coverage(monkeypatch, covers=False)

    def build(start, end, *, severities=None, progress=None):
        calls.append(f"build:{start}..{end}")
        assert progress is not None, "a multi-minute build must report progress"
        progress("Indexing in place.")
        return {"build_seconds": 465.0, "rows": 1_401_135}

    monkeypatch.setattr(external, "build_external_index", build)
    monkeypatch.setattr(specs, "search_with_total",
                        lambda *a: calls.append("search") or [])
    _, events = _search(
        query_text="shipping quote", dataset="otel_slice",
        category="error", start_time=WINDOW[0], end_time=WINDOW[1], limit=2,
    )
    assert calls == [f"build:{WINDOW[0]}..{WINDOW[1]}", "search"]
    assert any(e.get("status") == "working" for e in events)


def test_searching_a_covered_window_builds_nothing(monkeypatch):
    from backend.pipeline.tools import specs

    _coverage(monkeypatch, covers=True)
    _never_builds(monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(specs, "search_with_total",
                        lambda *a: calls.append("search") or [])
    _search(
        query_text="shipping quote", dataset="otel_slice",
        start_time=WINDOW[0], end_time=WINDOW[1], limit=2,
    )
    assert calls == ["search"]


def test_a_local_corpus_search_never_touches_the_otel_index(monkeypatch):
    from backend.pipeline.tools import specs

    monkeypatch.setattr(
        external, "ensure_searchable",
        lambda *a, **k: pytest.fail("only otel_slice is indexed in place"),
    )
    calls: list[str] = []
    monkeypatch.setattr(specs, "search_with_total",
                        lambda *a: calls.append("search") or [])
    _search(query_text="timeout", dataset="openstack", limit=2)
    assert calls == ["search"]


@pytest.mark.parametrize("effort_free", ["Low", "Medium", "High", "Extreme", "Brutal"])
def test_indexing_is_not_conditional_on_anything(monkeypatch, effort_free):
    """There is no effort gate: the same call indexes regardless of context."""
    built: list[str] = []
    _coverage(monkeypatch, covers=False)
    monkeypatch.setattr(
        external, "build_external_index",
        lambda start, end, **k: built.append(start) or {"build_seconds": 1.0, "rows": 1},
    )
    external.ensure_searchable(*WINDOW)
    assert built == [WINDOW[0]]


# --- The one case that cannot be answered ----------------------------------
def test_a_search_with_no_window_and_no_index_is_refused_with_the_fix(monkeypatch):
    _coverage(monkeypatch, exists=False)
    _never_builds(monkeypatch)
    with pytest.raises(QueryRefused) as caught:
        external.ensure_searchable(None, None)
    refusal = caught.value.refusal
    assert refusal.code == "index_window_required"
    assert "start_time and end_time" in refusal.suggestion


def test_a_search_with_no_window_uses_whatever_index_exists(monkeypatch):
    _coverage(monkeypatch, exists=True)
    _never_builds(monkeypatch)
    result = external.ensure_searchable(None, None)
    assert result["built"] is False and result["exists"] is True


# --- The tool ---------------------------------------------------------------
def test_the_tool_declares_a_non_measured_kind():
    assert TOOL_KINDS["otel_index_tool"] == "catalog"
    assert "catalog" not in MEASURED_KINDS


def test_the_tool_takes_a_window_and_a_release_flag():
    assert set(otel_index_tool.tool_call_schema.model_fields) == {
        "start_time", "end_time", "release",
    }


def test_the_tool_reports_coverage_without_building(monkeypatch):
    _coverage(monkeypatch, covers=False)
    _never_builds(monkeypatch)
    row = _row(_invoke("otel_index_tool")[0])
    assert row["built"] is False
    assert row["window_start"] == WINDOW[0]
    assert row["exists"] is True


def test_the_tool_indexes_the_window_it_is_given(monkeypatch):
    _coverage(monkeypatch, covers=False)
    monkeypatch.setattr(
        external, "build_external_index",
        lambda *a, **k: {"build_seconds": 465.0, "rows": 1_401_135, "bytes": 62},
    )
    row = _row(_invoke(
        "otel_index_tool", start_time=WINDOW[0], end_time=WINDOW[1],
    )[0])
    assert row["built"] is True
    assert row["build_seconds"] == 465.0
    assert row["indexed_rows"] == 1_401_135


def test_a_half_given_window_is_rejected_by_the_schema():
    from pydantic import ValidationError

    from backend.pipeline.tools.schemas import OtelIndex

    with pytest.raises(ValidationError):
        OtelIndex(start_time=WINDOW[0])
    with pytest.raises(ValidationError):
        OtelIndex(start_time=WINDOW[1], end_time=WINDOW[0])


# --- No SAVEPOINT or advisory lock, which SereneDB does not accept ----------
def test_the_index_path_executes_no_savepoint_or_advisory_lock():
    """`SAVEPOINT otel_advisory_lock` raised a SereneDB syntax error mid-build,
    and wrapping it in try/except could not help: the savepoint statement is
    itself what failed. Matched on executed SQL, not on prose."""
    import re
    from pathlib import Path

    source = Path("src/backend/db/external.py").read_text(encoding="utf-8")
    for statement in re.findall(r"""execute\(\s*[\"']([^\"']+)""", source):
        assert "savepoint" not in statement.lower(), statement
        assert "advisory" not in statement.lower(), statement


def test_builds_are_serialized():
    """Two concurrent searches must not index at the same time."""
    import threading

    assert isinstance(external._BUILD_LOCK, type(threading.Lock()))


# --- The model is told how this works --------------------------------------
def test_the_catalog_describes_automatic_indexing_without_an_effort_gate():
    from backend.pipeline.prompts.builders import COST_DISCIPLINE, TOOL_CATALOG

    description = TOOL_CATALOG["otel_index_tool"]
    assert "TIER" in description
    search = TOOL_CATALOG["search_logs_tool"]
    assert "automatic" in search.lower() or "automatically" in search.lower()
    for text in (description, search, COST_DISCIPLINE):
        assert "Extreme/Brutal may build" not in text
        assert "confirm_cost" not in text


def test_the_mcp_transport_carries_the_same_tool(monkeypatch):
    """Both transports are generated from the one spec, so the server's own
    code path must answer the same arguments."""
    import json

    from backend.mcp_server.tools import call_spec
    from backend.pipeline.tools import mcp_tools, specs
    from tool_invoke import tool_call

    sent: dict = {}

    class Bridge:
        def call_tool(self, name, arguments):
            sent.update(name=name, arguments=arguments)
            return call_spec(specs.BY_NAME[name], arguments)

    monkeypatch.setattr(
        external, "index_coverage",
        lambda *a, **k: {"index": external.INDEX_NAME, "exists": True,
                         "covers": False, "window_start": WINDOW[0],
                         "window_end": WINDOW[1], "severities": ["error"],
                         "index_bytes": 5 << 20},
    )
    monkeypatch.setattr(mcp_tools, "get_mcp_bridge", lambda: Bridge())
    tool = mcp_tools.MCP_TOOLS["otel_index_tool"]
    events: list = []
    message = tool.invoke(tool_call(tool, events))
    result = json.loads(message.content)

    assert sent["name"] == "otel_index_tool"
    assert sent["arguments"]["release"] is False
    assert result["kind"] == "catalog" and result["dataset"] == "otel_slice"
    assert result["data"][0]["window_start"] == WINDOW[0]


def test_the_tool_is_declared_once_for_every_transport():
    from backend.pipeline.tools import mcp_tools, specs
    from backend.pipeline.tools.local import LOCAL_TOOLS

    spec = specs.BY_NAME["otel_index_tool"]
    assert spec.kind == "catalog"
    # A build creates and drops an index and runs for minutes, so it is neither
    # read-only nor bound by the default call timeout.
    assert spec.read_only is False
    assert spec.timeout is None
    assert spec.reports_progress is True
    assert "otel_index_tool" in LOCAL_TOOLS
    assert "otel_index_tool" in mcp_tools.MCP_TOOLS


def test_search_may_build_so_it_reports_progress_and_has_no_call_timeout():
    """A search on otel_slice can spend minutes indexing; a 45s server timeout
    would abort the build it just started."""
    from backend.pipeline.tools import specs

    spec = specs.BY_NAME["search_logs_tool"]
    assert spec.reports_progress is True
    assert spec.timeout is None


# --- Reclaiming disk, because the budget is small -------------------------
def test_a_replacing_build_is_not_charged_for_the_index_it_frees(monkeypatch):
    """Only one index exists at a time: the old one is dropped before the new
    one is created. Charging the cap for both at once refused builds for space
    the build itself was about to free."""
    from backend.db import budget

    seen: dict = {}

    def fake_fits(estimated, *, dataset_id=None, current_db_bytes=None):
        seen["current_db_bytes"] = current_db_bytes
        return budget.BudgetVerdict(
            ok=True, reason="fits", estimated_bytes=estimated,
            allowance_bytes=1 << 40, suggestion="",
        )

    @contextlib.contextmanager
    def cursor():
        class Cur:
            def execute(self, statement, params=None):
                pass
            def fetchone(self):
                return {"b": 400 * 1024 * 1024, "index_bytes": 36 * 1024 * 1024, "n": 1}
            def fetchall(self):
                return []
        yield Cur()

    monkeypatch.setattr(external, "_cursor", cursor)
    monkeypatch.setattr(external, "fits", fake_fits)
    plan = external.plan_external_index(*PLAN_WINDOW)
    external.build_external_index(*PLAN_WINDOW, plan=plan, dry_run=True)

    # 400 MiB of database minus the 36 MiB the replaced index gives back.
    assert seen["current_db_bytes"] == (400 - 36) * 1024 * 1024


def test_a_dry_run_reports_what_it_could_reclaim(monkeypatch):
    @contextlib.contextmanager
    def cursor():
        class Cur:
            def execute(self, statement, params=None):
                pass
            def fetchone(self):
                return {"b": 400 * 1024 * 1024, "index_bytes": 36 * 1024 * 1024}
            def fetchall(self):
                return []
        yield Cur()

    monkeypatch.setattr(external, "_cursor", cursor)
    report = external.build_external_index(*PLAN_WINDOW, dry_run=True)
    assert report["reclaimable_bytes"] == 36 * 1024 * 1024


def test_the_build_record_keeps_the_measured_size():
    """SereneDB will not size an external index on request, so the figure has
    to be stored at build time or it is lost."""
    plan = external.plan_external_index(*PLAN_WINDOW)
    statements = external.record_build_statements(plan, rows=1000, index_bytes=4096)
    create = str(statements[1][0])
    insert_params = statements[2][1]
    assert "index_bytes" in create
    assert 4096 in insert_params


def test_release_drops_the_index_and_reports_the_disk_returned(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        external, "index_coverage",
        lambda *a, **k: {"exists": True, "window_start": WINDOW[0],
                         "window_end": WINDOW[1], "index_bytes": 1024},
    )
    monkeypatch.setattr(
        external, "drop_external_index",
        lambda: calls.append("drop") or {"freed_bytes": 37_000_000,
                                         "db_bytes": 390_000_000},
    )
    result = external.release_external_index()
    assert calls == ["drop"]
    assert result["released"] is True
    assert result["freed_bytes"] == 37_000_000
    assert "rebuild" in result["note"]


def test_release_is_serialized_against_builds():
    """A release landing in the middle of a build would drop the view from
    under it, so both take the same lock."""
    import inspect

    source = inspect.getsource(external.release_external_index)
    assert "_BUILD_LOCK" in source


def test_the_tool_releases_on_request(monkeypatch):
    monkeypatch.setattr(
        external, "release_external_index",
        lambda: {"released": True, "freed_bytes": 37_000_000,
                 "db_bytes": 390_000_000, "note": "rebuilds"},
    )
    monkeypatch.setattr(
        external, "ensure_external_index",
        lambda *a, **k: pytest.fail("release must not build"),
    )
    row = _row(_invoke("otel_index_tool", release=True)[0])
    assert row["released"] is True and row["freed_bytes"] == 37_000_000


def test_releasing_and_indexing_in_one_call_is_rejected():
    """The two are opposites; a single call asking for both is a mistake."""
    from pydantic import ValidationError

    from backend.pipeline.tools.schemas import OtelIndex

    with pytest.raises(ValidationError):
        OtelIndex(start_time=WINDOW[0], end_time=WINDOW[1], release=True)


def test_the_catalog_tells_the_model_that_releasing_costs_a_rebuild():
    from backend.pipeline.prompts.builders import TOOL_CATALOG

    description = TOOL_CATALOG["otel_index_tool"]
    assert "release" in description
    assert "rebuild" in description.lower()


# --- Memory, which is what actually killed the builds ----------------------
@pytest.mark.parametrize(
    "text, expected",
    [
        ("5.7 GiB", int(5.7 * 1024**3)),
        ("512MB", 512_000_000),
        ("488.2 MiB", int(488.2 * 1024**2)),
        ("1024", 1024),
        ("nonsense", None),
        ("", None),
    ],
)
def test_sizes_are_read_as_serenedb_reports_them(text, expected):
    assert external.parse_byte_size(text) == expected


def test_a_ceiling_above_the_budget_is_lowered_before_a_build(monkeypatch):
    """A restarted container came back at 5.7 GiB -- the value the budget
    records as the cause of three OOM kills -- and the index build is what
    breaches it."""
    applied: list[str] = []
    monkeypatch.setattr("backend.db.connection.current_memory_limit", lambda: "5.7 GiB")
    monkeypatch.setattr(
        "backend.db.connection.set_global_memory_limit",
        lambda *a, **k: applied.append("set") or "488.2 MiB",
    )
    result = external.hold_memory_ceiling()
    assert applied == ["set"]
    assert result["changed"] is True
    assert result["previous"] == "5.7 GiB"


@pytest.mark.parametrize("already", ["488.2 MiB", "512MB", "128 MiB"])
def test_a_ceiling_at_or_below_the_budget_is_left_alone(monkeypatch, already):
    """A deliberately tighter setting must never be raised."""
    monkeypatch.setattr("backend.db.connection.current_memory_limit", lambda: already)
    monkeypatch.setattr(
        "backend.db.connection.set_global_memory_limit",
        lambda *a, **k: pytest.fail("a limit within budget must not be touched"),
    )
    assert external.hold_memory_ceiling()["changed"] is False


def test_an_unreadable_ceiling_never_blocks_the_build(monkeypatch):
    monkeypatch.setattr(
        "backend.db.connection.current_memory_limit",
        lambda: (_ for _ in ()).throw(RuntimeError("no connection")),
    )
    result = external.hold_memory_ceiling()
    assert result["changed"] is False and "error" in result


def test_a_priced_plan_stays_read_only(monkeypatch):
    """confirm-before-you-spend must not reconfigure the server."""
    monkeypatch.setattr(
        external, "hold_memory_ceiling",
        lambda: pytest.fail("a dry run must not touch the memory ceiling"),
    )

    @contextlib.contextmanager
    def cursor():
        class Cur:
            def execute(self, statement, params=None):
                pass
            def fetchone(self):
                return {"b": 400 * 1024 * 1024, "index_bytes": 0}
            def fetchall(self):
                return []
        yield Cur()

    monkeypatch.setattr(external, "_cursor", cursor)
    assert external.build_external_index(*PLAN_WINDOW, dry_run=True)["dry_run"] is True
