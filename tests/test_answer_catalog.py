"""Offline tests for the answer prompt's tool-result catalog."""

import os

os.environ.setdefault("TYPESAFE_API_KEY", "test-key")

from backend.pipeline.answers import render_appendix
from backend.pipeline.prompts import builders
from backend.pipeline.prompts.builders import (
    ANSWER_EXAMPLE_ROWS,
    ANSWER_MEASURED_ROWS,
    build_result_catalog,
)
from backend.pipeline.tools.results import MAX_RESULT_ROWS


def _record(rid, kind, n, *, cell="x", **extra):
    rows = [{"category": f"c{i}", "count": i, "text": cell} for i in range(n)]
    return {
        "id": rid,
        "tool_name": f"{kind}_tool",
        "kind": kind,
        "arguments": {},
        "row_count": n,
        "rows": rows[:MAX_RESULT_ROWS],
        "truncated": False,
        **extra,
    }


def test_result_rows_cap_matches_largest_tool_cap():
    assert MAX_RESULT_ROWS == ANSWER_MEASURED_ROWS == 100


def test_short_aggregate_appears_whole():
    text = build_result_catalog([_record("R1", "aggregate", 10)]).text
    assert "rows shown: 10 of 10" in text
    for i in range(10):
        assert f"category: c{i}," in text


def test_search_shows_example_rows_and_what_is_missing():
    text = build_result_catalog([_record("R1", "search", 60)]).text
    assert f"rows shown: {ANSWER_EXAMPLE_ROWS} of 60" in text
    assert f"category: c{ANSWER_EXAMPLE_ROWS - 1}," in text
    assert f"category: c{ANSWER_EXAMPLE_ROWS}," not in text


def test_long_trend_shows_measured_cap_and_what_is_missing():
    # The record carries true row_count 150 but only the first 100 rows.
    record = _record("R1", "trend", 150)
    text = build_result_catalog([record]).text
    assert "rows shown: 100 of 150" in text
    assert "category: c99," in text and "category: c100," not in text


def test_truncated_flag_is_kept():
    record = _record("R1", "search", 5, truncated=True)
    assert "truncated: True" in build_result_catalog([record]).text


def test_over_budget_shrinks_examples_newest_first_and_keeps_measured_rows():
    measured = _record("R1", "aggregate", 30)
    older = _record("R2", "search", 30, cell="y" * 200)
    newer = _record("R3", "search", 30, cell="z" * 200)
    full = build_result_catalog([measured, older, newer]).text
    # A budget that fits after shrinking only the newest example.
    shrunk_newer = builders._catalog_entry(newer, 1)
    budget = len(full) - (len(builders._catalog_entry(newer, 10)) - len(shrunk_newer))
    catalog = build_result_catalog([measured, older, newer], char_budget=budget)
    assert catalog.notes == ()
    assert "rows shown: 30 of 30" in catalog.text  # measured untouched
    assert "rows shown: 1 of 30" in catalog.text  # newest example shrunk
    assert catalog.text.count(f"rows shown: {ANSWER_EXAMPLE_ROWS} of 30") == 1  # older kept


def test_measured_rows_alone_over_budget_are_kept_with_a_note(caplog):
    measured = _record("R1", "aggregate", 50, cell="m" * 200)
    with caplog.at_level("WARNING"):
        catalog = build_result_catalog([measured], char_budget=100)
    assert "rows shown: 50 of 50" in catalog.text
    assert len(catalog.notes) == 1 and "over the 100 budget" in catalog.notes[0]
    assert "over budget" in caplog.text
    assert "Note: " + catalog.notes[0] in render_appendix([measured], catalog.notes)


def test_derived_shows_its_formula_and_all_rows():
    record = _record("R4", "derived", 3, formula="0.4*volume + 0.6*errors")
    text = build_result_catalog([record]).text
    assert "formula: 0.4*volume + 0.6*errors" in text
    assert "rows shown: 3 of 3" in text


def test_cells_are_cut_with_an_explicit_marker():
    text = build_result_catalog([_record("R1", "search", 1, cell="a" * 500)]).text
    assert "…[cut]" in text


def test_no_pipe_separators_in_row_lines():
    text = build_result_catalog([_record("R1", "aggregate", 3)]).text
    assert "|" not in text
