"""Cost accounting and its presentation in the answer.

Offline and pure. Nothing here asks the model for a number: the totals come from
the query layer's own ``query_stats``, which is the point -- a figure the model
never produces is a figure it cannot get wrong.
"""

from __future__ import annotations

import pytest

from backend.pipeline.answers import serialize_answer
from backend.pipeline.cost import (
    collect_stats,
    format_cost_note,
    investigation_cost,
)
from ui.rendering import cost_rows, cost_total, render_answer


def result(result_id: str, rows: int = 3, **stats) -> dict:
    """A tool result carrying query_stats, as ``tools/results.py`` builds it."""
    base = {
        "dataset": "otel_s3",
        "query_class": "bulk",
        "approximate": True,
        "files_considered": 1,
        "files_pruned": 0,
        "row_groups_considered": 1624,
        "row_groups_read": 10,
        "est_rows_scanned": 6_176_768,
        "est_bytes_read": 229_200_000,
        "elapsed_ms": 1003,
        "row_count": rows,
        "notes": [],
    }
    base.update(stats)
    return {
        "id": result_id,
        "tool_name": "count_by_category_tool",
        "arguments": {"dataset": "otel_s3"},
        "row_count": rows,
        "rows": [{"severity": "error", "count": 1}] * rows,
        "query_stats": base,
    }


# --- Collection -------------------------------------------------------------
def test_stats_are_collected_from_tool_results():
    collected = collect_stats([result("R1"), result("R2")])
    assert [rid for rid, _ in collected] == ["R1", "R2"]
    assert collected[0][1].est_rows_scanned == 6_176_768


def test_results_without_stats_are_ignored_not_guessed_at():
    plain = {"id": "R1", "row_count": 2, "rows": []}
    assert collect_stats([plain]) == []


def test_unknown_stat_keys_are_dropped_rather_than_crashing():
    """Forward compatibility: an older persisted message may carry extra keys."""
    record = result("R1")
    record["query_stats"]["some_future_field"] = 1
    collected = collect_stats([record])
    assert len(collected) == 1


# --- Aggregation ------------------------------------------------------------
def test_investigation_cost_totals_every_query():
    cost = investigation_cost([result("R1"), result("R2", rows=5)])
    total = cost["total"]

    assert total["queries"] == 2
    assert total["rows_scanned"] == 2 * 6_176_768
    assert total["elapsed_ms"] == 2 * 1003
    assert total["rows_returned"] == 8


def test_a_single_approximate_query_makes_the_total_approximate():
    cost = investigation_cost(
        [result("R1", approximate=False), result("R2", approximate=True)]
    )
    assert cost["total"]["approximate"] is True
    assert cost["approximate_count"] == 1


def test_empty_input_yields_a_valid_empty_shape():
    """Callers must never have to branch on presence."""
    cost = investigation_cost([])
    assert cost["queries"] == []
    assert cost["total"] is None
    assert cost["note"] == ""


def test_pruning_ratio_is_reported_per_query():
    cost = investigation_cost([result("R1")])
    # 10 of 1,624 row groups read.
    assert cost["queries"][0]["pruning_ratio"] > 0.99


def test_usefulness_distinguishes_an_empty_result_from_a_populated_one():
    """A query that read millions and returned nothing is a true negative."""
    cost = investigation_cost([result("R1", rows=0), result("R2", rows=4)])
    assert cost["queries"][0]["useful"] is False
    assert cost["queries"][1]["useful"] is True


def test_datasets_queried_are_listed():
    cost = investigation_cost([result("R1"), result("R2", dataset="openstack")])
    assert set(cost["datasets"]) == {"otel_s3", "openstack"}


# --- The note that reaches the answer ---------------------------------------
def test_the_cost_note_leads_with_timing():
    """Timing first, because it is what a reader uses to judge efficiency."""
    note = investigation_cost([result("R1"), result("R2")])["note"]
    assert note.startswith("Query cost: 2 queries in ")
    assert "rows scanned" in note
    assert "MiB" in note
    assert "approximate" in note


def test_the_note_names_the_slowest_query():
    """An average hides the shape of the work: eight fast aggregates and one
    slow text read average to something that describes neither."""
    note = investigation_cost(
        [result("R1", elapsed_ms=50), result("R2", elapsed_ms=5_000)]
    )["note"]
    assert "slowest R2" in note
    assert "% of the total" in note


def test_a_single_query_needs_no_slowest_breakdown():
    note = investigation_cost([result("R1")])["note"]
    assert "slowest" not in note


def test_the_note_reports_a_throughput_rate():
    """So "slow" can be told apart from "a lot of data"."""
    note = investigation_cost([result("R1")])["note"]
    assert "rows/s" in note


def test_timing_is_exposed_structurally_for_measurement():
    """A caller measuring agent performance should not have to parse prose."""
    timing = investigation_cost(
        [result("R1", elapsed_ms=100), result("R2", elapsed_ms=900)]
    )["timing"]

    assert timing["query_count"] == 2
    assert timing["total_ms"] == 1_000
    assert timing["slowest_ms"] == 900
    assert timing["slowest_id"] == "R2"
    assert timing["mean_ms"] == 500.0
    assert timing["rows_per_second"] > 0


def test_the_note_mentions_queries_that_returned_nothing():
    note = investigation_cost([result("R1", rows=0)])["note"]
    assert "returned no rows" in note


def test_an_exact_only_investigation_claims_no_approximation():
    note = investigation_cost(
        [result("R1", approximate=False, dataset="openstack")]
    )["note"]
    assert "approximate" not in note


def test_format_cost_note_is_empty_without_queries():
    from backend.db.stats import QueryStats

    assert format_cost_note([], QueryStats()) == ""


# --- Serialization ----------------------------------------------------------
def test_the_cost_note_is_appended_to_the_answer_text():
    answer = {"blocks": [{"kind": "text", "text": "Payments are failing."}]}
    text = serialize_answer(answer, set(), "Data queried: ~6,176,768 rows scanned.")

    assert text.startswith("Payments are failing.")
    assert text.endswith("Data queried: ~6,176,768 rows scanned.")


def test_serialization_without_a_note_is_unchanged():
    """Existing callers must keep working, so the note is optional."""
    answer = {"blocks": [{"kind": "text", "text": "All quiet."}]}
    assert serialize_answer(answer, set()) == "All quiet."


def test_the_note_does_not_disturb_result_markers():
    answer = {
        "blocks": [
            {"kind": "text", "text": "Top services:"},
            {"kind": "table", "result_id": "R1"},
        ]
    }
    text = serialize_answer(answer, {"R1"}, "Data queried: 1 query.")
    lines = [line for line in text.splitlines() if line.strip()]
    assert lines == ["Top services:", "[[RESULT:R1]]", "Data queried: 1 query."]


# --- UI panel ---------------------------------------------------------------
def test_cost_rows_badge_approximate_results():
    rows = cost_rows([result("R1", approximate=True), result("R2", approximate=False)])
    assert rows[0]["Exact"] == "~approx"
    assert rows[1]["Exact"] == "exact"


def test_cost_rows_format_volume_and_time():
    row = cost_rows([result("R1")])[0]
    assert row["Result"] == "R1"
    assert row["Rows scanned"] == "6,176,768"
    assert row["Read"].endswith("MiB")
    assert row["Time"] == "1003 ms"


def test_cost_rows_are_empty_when_nothing_reported_cost():
    assert cost_rows([{"id": "R1", "rows": []}]) == []
    assert cost_rows(None) == []


def test_cost_total_summarizes_the_investigation():
    total = cost_total([result("R1"), result("R2")])
    assert "2 queries" in total
    assert "rows scanned" in total
    assert "2 approximate" in total


def test_cost_total_is_empty_without_stats():
    assert cost_total([]) == ""


class FakeContainer:
    """Minimal stand-in for a Streamlit container."""

    def __init__(self) -> None:
        self.markdown_calls: list[str] = []
        self.dataframes: list = []
        self.expanders: list[str] = []

    def markdown(self, text: str) -> None:
        self.markdown_calls.append(text)

    def dataframe(self, frame, **_kwargs) -> None:
        self.dataframes.append(frame)

    def expander(self, label: str, **_kwargs):
        self.expanders.append(label)
        return self


def test_render_answer_shows_the_cost_panel():
    container = FakeContainer()
    render_answer(container, "Payments are failing.", [result("R1")])

    assert container.expanders
    assert "Data queried" in container.expanders[0]
    # One frame for the panel; the marker-less answer rendered no result table.
    assert len(container.dataframes) == 1


def test_render_answer_shows_no_panel_without_cost_data():
    container = FakeContainer()
    render_answer(container, "All quiet.", [{"id": "R1", "rows": []}])
    assert container.expanders == []


def test_render_answer_still_renders_tables_and_prose():
    container = FakeContainer()
    render_answer(container, "Top services:\n\n[[RESULT:R1]]", [result("R1")])

    assert any("Top services" in call for call in container.markdown_calls)
    # One result table plus the cost panel.
    assert len(container.dataframes) == 2
