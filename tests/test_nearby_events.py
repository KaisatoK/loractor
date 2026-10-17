"""Offline checks for ``get_nearby_events_tool`` (nearby events around a line).

Covers the argument schema, the SQL the builder composes (shape, parameters,
correlation filters, omitted branches), its refusals, the explanatory notes,
and the tool envelope on both transports. Live checks against the loaded
corpora are in ``test_openstack_live.py`` and ``test_bgl_live.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).parent))

from sql_render import normalized  # noqa: E402

import backend.db.context_query as context_query  # noqa: E402
from backend.db.errors import QueryRefused  # noqa: E402
from backend.db.stats import QueryResult, QueryStats  # noqa: E402
from backend.pipeline.tools.schemas import GetNearbyEvents  # noqa: E402


def build(**kwargs):
    return context_query.nearby_events.__wrapped__(**kwargs)


# --- Schema -------------------------------------------------------------------
def test_nearby_events_defaults_and_bounds():
    args = GetNearbyEvents(line_id=7)
    assert (args.before, args.after) == (5, 5)
    assert args.same_entity is False and args.same_request is False
    for bad in ({"line_id": 0}, {"line_id": 7, "before": 51},
                {"line_id": 7, "after": -1}, {"line_id": 7, "dataset": "t"}):
        with pytest.raises(ValidationError):
            GetNearbyEvents(**bad)


def test_both_correlation_flags_can_be_combined():
    args = GetNearbyEvents(line_id=7, same_entity=True, same_request=True)
    assert args.same_entity and args.same_request


# --- SQL shape ----------------------------------------------------------------
def test_the_anchor_id_is_the_only_parameter():
    query, params, stats = build(line_id=42, dataset="openstack")
    assert params == [42]
    assert stats.dataset == "openstack" and stats.approximate is False
    text = normalized(query)
    assert 'WITH anchor AS ( SELECT "LineId" AS rid FROM "openstack" WHERE "LineId" = %s )' in text
    # The base table is the scan relation, never the search index.
    assert "openstack_idx" not in text


def test_both_sides_are_bounded_branches_numbered_outward():
    text = normalized(build(line_id=42, dataset="openstack", before=3, after=7)[0])
    assert text.count("UNION ALL") == 2
    assert '"LineId" < a.rid ORDER BY "LineId" DESC LIMIT 3' in text
    assert '"LineId" > a.rid ORDER BY "LineId" ASC LIMIT 7' in text
    assert '-ROW_NUMBER() OVER (ORDER BY s.record_id DESC) AS "offset"' in text
    assert 'ROW_NUMBER() OVER (ORDER BY s.record_id ASC) AS "offset"' in text
    assert '0 AS "offset"' in text
    assert text.endswith("ORDER BY record_id")


def test_a_zero_side_is_left_out_of_the_statement():
    before_only = normalized(build(line_id=42, dataset="openstack", after=0)[0])
    assert before_only.count("UNION ALL") == 1 and "a.rid ORDER BY" in before_only
    assert '> a.rid' not in before_only
    anchor_only = normalized(build(line_id=42, dataset="openstack", before=0, after=0)[0])
    assert "UNION ALL" not in anchor_only and "ROW_NUMBER" not in anchor_only


def test_rows_carry_the_canonical_columns_the_corpus_has():
    openstack = normalized(build(line_id=1, dataset="openstack")[0])
    for alias in ("record_id", "ts", "severity", "service", "entity_id",
                  "request_id", "message", "template"):
        assert f'AS "{alias}"' in openstack, alias
    bgl = normalized(build(line_id=1, dataset="bgl")[0])
    assert 'AS "request_id"' not in bgl and '"Node" AS "entity_id"' in bgl


def test_same_entity_narrows_both_sides_to_the_anchors_entity():
    query, params, _ = build(line_id=42, dataset="bgl", same_entity=True)
    text = normalized(query)
    assert '"Node" AS ent' in text
    assert text.count('AND "Node" = a.ent') == 2
    assert params == [42]


def test_same_request_narrows_both_sides_to_the_anchors_request():
    text = normalized(build(line_id=42, dataset="openstack", same_request=True)[0])
    assert "AS req FROM" in text and text.count("= a.req") == 2
    assert "a.ent" not in text


def test_the_flags_combine():
    text = normalized(build(line_id=42, dataset="openstack",
                            same_entity=True, same_request=True)[0])
    assert text.count('AND "Pid" = a.ent') == 2 and text.count("= a.req") == 2


# --- Refusals -------------------------------------------------------------------
@pytest.mark.parametrize("dataset", ["otel_s3", "otel_slice"])
def test_a_corpus_without_a_record_id_is_refused(dataset):
    with pytest.raises(QueryRefused) as excinfo:
        build(line_id=1, dataset=dataset)
    assert excinfo.value.refusal.code == "unsupported_capability"
    assert "openstack" in excinfo.value.refusal.suggestion


def test_same_request_is_refused_where_there_is_no_request_id():
    with pytest.raises(QueryRefused) as excinfo:
        build(line_id=1, dataset="bgl", same_request=True)
    refusal = excinfo.value.refusal
    assert refusal.code == "unsupported_field"
    assert "same_entity" in refusal.suggestion and "openstack" in refusal.suggestion


def test_more_than_fifty_neighbours_is_refused():
    with pytest.raises(QueryRefused):
        build(line_id=1, dataset="openstack", before=51)


# --- Notes ----------------------------------------------------------------------
def _stub(monkeypatch, rows):
    monkeypatch.setattr(
        context_query, "nearby_events",
        lambda *a, **k: QueryResult([dict(r) for r in rows],
                                    QueryStats(dataset="openstack")),
    )
    monkeypatch.setattr(
        context_query, "request_context_summary",
        lambda *a, **k: QueryResult([], QueryStats(dataset="openstack")),
    )


def test_an_unknown_anchor_is_an_empty_success_with_a_note(monkeypatch):
    _stub(monkeypatch, [])
    result = context_query.get_nearby_events(999_999, "openstack")
    assert list(result) == [] and not result.refused
    assert "No line has record_id 999999" in result.stats.notes[0]


def test_an_anchor_without_the_flagged_value_says_so(monkeypatch):
    _stub(monkeypatch, [{"record_id": 5, "offset": 0, "entity_id": 7,
                         "request_id": None}])
    result = context_query.get_nearby_events(5, "openstack", same_request=True)
    assert result.stats.notes == [
        "The anchor line has no request_id, so same_request matches no other "
        "line; call again without same_request for its neighbours."
    ]


def test_an_anchor_alone_in_its_request_says_so(monkeypatch):
    _stub(monkeypatch, [{"record_id": 5, "offset": 0, "entity_id": 7,
                         "request_id": "req-1"}])
    result = context_query.get_nearby_events(5, "openstack", same_request=True)
    assert result.stats.notes == [
        "No other line in 'openstack' shares the anchor's request_id."
    ]


def test_an_ordinary_timeline_carries_no_note(monkeypatch):
    _stub(monkeypatch, [{"record_id": 4, "offset": -1}, {"record_id": 5, "offset": 0},
                        {"record_id": 6, "offset": 1}])
    assert context_query.get_nearby_events(5, "openstack").stats.notes == []


# --- Tool ----------------------------------------------------------------------
class _Runtime:
    def __init__(self):
        self.events = []
        self.stream_writer = self.events.append


def test_request_context_summary_builder_counts_span_and_components():
    query, params, stats = context_query.request_context_summary.__wrapped__(
        "req-1", dataset="openstack"
    )
    text = normalized(query)
    assert "count(*) AS request_rows" in text
    assert "min(\"Timestamp\") AS request_first_ts" in text
    assert "max(\"Timestamp\") AS request_last_ts" in text
    assert 'count(DISTINCT "Component") AS request_components' in text
    assert "= %s" in text and params == ["req-1"]
    assert stats.dataset == "openstack"


def test_same_request_adds_a_measured_context_summary(monkeypatch):
    monkeypatch.setattr(
        context_query, "nearby_events",
        lambda *a, **k: QueryResult(
            [{"record_id": 5, "offset": 0, "request_id": "req-1"}],
            QueryStats(dataset="openstack"),
        ),
    )
    monkeypatch.setattr(
        context_query, "request_context_summary",
        lambda *a, **k: QueryResult(
            [{
                "request_rows": 52_349,
                "request_first_ts": "2017-05-14 19:39:09+00:00",
                "request_last_ts": "2017-05-17 12:02:35+00:00",
                "request_components": 5,
            }],
            QueryStats(dataset="openstack"),
        ),
    )
    result = context_query.get_nearby_events(5, "openstack", same_request=True)
    assert any("52,349 log entries" in note for note in result.stats.notes)
    assert any("periodic service task" in note for note in result.stats.notes)


def _call_tool(monkeypatch, rows, **arguments):
    from backend.pipeline.tools import get_nearby_events_tool, specs
    from backend.pipeline.tools.schemas import GetNearbyEvents

    seen = {}

    def fake(*args):
        seen["args"] = args
        return QueryResult([dict(r) for r in rows], QueryStats(dataset="openstack"))

    # One patch covers every transport: the spec's run reads it from ``specs``.
    monkeypatch.setattr(specs, "get_nearby_events", fake)
    runtime = _Runtime()
    validated = GetNearbyEvents(**arguments).model_dump()
    envelope = get_nearby_events_tool.func(runtime=runtime, **validated)
    return envelope, seen["args"], runtime.events


def test_the_tool_returns_a_context_envelope(monkeypatch):
    rows = [{"record_id": 4, "offset": -1}, {"record_id": 5, "offset": 0}]
    envelope, args, events = _call_tool(
        monkeypatch, rows, line_id=5, dataset="openstack", before=1, after=0,
        same_entity=True,
    )
    assert args == (5, "openstack", 1, 0, True, False)
    assert envelope["status"] == "success"
    assert envelope["kind"] == "context" and envelope["dataset"] == "openstack"
    assert envelope["filters"] == {"line_id": 5, "before": 1, "after": 0,
                                   "same_entity": True, "same_request": False}
    assert envelope["returned"] == 2 and envelope["truncated"] is False
    assert [e["status"] for e in events] == ["started", "completed"]
    assert "with the same entity" in events[0]["operation"]
