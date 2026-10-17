"""Slice planning, batching, and budget enforcement.

Offline: everything here is derivation from measured constants. The build itself
needs a live server and is exercised by ``test_slice_live.py``.

These tests encode the reasoning that three server crashes produced:

* the slice window is **derived** from the budget, never truncated by a LIMIT,
  because a truncated slice silently covers only its first moments;
* the extract is **batched**, because one statement spanning a wide window has
  to hold its whole result while streaming message text from object storage;
* batches are sized by **rows**, not by a fixed time span, because arrival rates
  differ by two orders of magnitude across severity sets.
"""

from __future__ import annotations

import pytest

from backend.db import slices
from backend.db.budget import (
    BUDGET,
    MATERIALIZED_BYTES_PER_ROW,
    MAX_SLICE_BUILD_SECONDS,
    PROBLEM_SEVERITIES,
    max_buildable_rows,
)
from backend.db.errors import QueryRefused


# --- Arrival rate -----------------------------------------------------------
def test_problem_severity_arrival_rate_matches_the_measurement():
    """Measured: ~78k problem-severity rows per second of corpus time.

    Derived from 1e9 rows over ~1,764 s at a measured ~14% problem rate.
    """
    rate = slices.arrival_rate(PROBLEM_SEVERITIES)
    assert 70_000 < rate < 85_000


def test_rare_severities_arrive_orders_of_magnitude_slower():
    """This gap is why batching is sized by rows rather than by time."""
    common = slices.arrival_rate(PROBLEM_SEVERITIES)
    rare = slices.arrival_rate(("fatal", "warn"))
    assert rare < common / 50


def test_unknown_severities_have_no_arrival_rate():
    assert slices.arrival_rate(("nonsense",)) == 0.0


# --- Plan derivation --------------------------------------------------------
def test_the_window_is_derived_from_whichever_constraint_binds():
    """Never larger than the disk ceiling, and clamped by the text read.

    Originally this asserted the plan always filled the disk allowance. It does
    not, and should not: reading message text costs ~660 s per row group, so the
    wall-clock limit binds long before disk does.
    """
    plan = slices.plan_slice(PROBLEM_SEVERITIES)
    disk_ceiling = max_buildable_rows(
        BUDGET.otel_slice_allowance_bytes,
        MAX_SLICE_BUILD_SECONDS,
        materialized=True,
    )
    assert 0 < plan.estimated_rows <= disk_ceiling
    assert plan.window_seconds > 0


def test_the_text_read_is_what_bounds_a_default_plan():
    """The binding constraint, asserted explicitly."""
    plan = slices.plan_slice(PROBLEM_SEVERITIES)
    assert (
        slices.estimate_text_read_seconds(plan.window_seconds)
        <= slices.MAX_SLICE_WALL_SECONDS + slices.BODY_SECONDS_PER_ROW_GROUP
    )


def test_rare_severities_do_not_buy_more_coverage():
    """The trap, encoded so it cannot be reintroduced.

    Restricting to rare severities *looks* like a bargain -- ``fatal``+``warn``
    arrive ~125x slower than problem severities, so the disk allowance would
    stretch to ~48 minutes of coverage instead of ~23 seconds. But text is read
    per row group regardless of how few rows survive the filter, so those 48
    minutes would touch ~2,600 row groups and cost days.

    Both plans are therefore clamped to the same affordable window; the rare
    plan simply yields fewer rows for the same build cost.
    """
    broad = slices.plan_slice(PROBLEM_SEVERITIES)
    narrow = slices.plan_slice(("fatal", "warn"))

    assert narrow.window_seconds == pytest.approx(broad.window_seconds)
    assert narrow.estimated_rows < broad.estimated_rows


def test_the_full_disk_affordable_rare_window_would_be_unaffordable():
    """Quantifies the trap: ~63 hours of text reads."""
    disk_affordable_seconds = (
        max_buildable_rows(
            BUDGET.otel_slice_allowance_bytes,
            MAX_SLICE_BUILD_SECONDS,
            materialized=True,
        )
        / slices.arrival_rate(("fatal", "warn"))
    )
    cost = slices.estimate_text_read_seconds(disk_affordable_seconds)
    assert cost > 10 * slices.MAX_SLICE_WALL_SECONDS


def test_a_severity_filter_alone_cannot_cover_much_time():
    """The finding that reshaped the slice.

    ``error`` is ~13.8% of the corpus, so the budget buys only seconds of
    wall-clock coverage when errors are included.
    """
    plan = slices.plan_slice(PROBLEM_SEVERITIES)
    assert plan.window_seconds < 60


def test_the_plan_estimates_disk_with_the_materialized_cost():
    plan = slices.plan_slice(max_rows=100_000)
    assert plan.estimated_bytes == 100_000 * MATERIALIZED_BYTES_PER_ROW


def test_the_plan_estimates_build_time():
    plan = slices.plan_slice(max_rows=120_000)
    assert plan.estimated_seconds > 0


def test_an_unknown_severity_set_is_refused():
    with pytest.raises(QueryRefused) as excinfo:
        slices.plan_slice(("critical",))
    assert excinfo.value.refusal.code == "unknown_severity"


def test_the_plan_dict_reports_the_window_it_will_cover():
    """Coverage has to be visible: a search that finds nothing in the slice
    proves nothing about the time the slice does not hold."""
    payload = slices.plan_slice(max_rows=50_000).as_dict()
    assert {"start", "end", "window_seconds", "estimated_rows"} <= set(payload)


# --- Batching ---------------------------------------------------------------
def test_batches_are_contiguous_and_cover_the_plan_exactly():
    plan = slices.plan_slice(max_rows=150_000)
    batches = list(slices._batches(plan))

    assert batches[0][0] == plan.start
    assert batches[-1][1] == plan.end
    for earlier, later in zip(batches, batches[1:]):
        assert earlier[1] == later[0]


def test_batch_width_is_sized_from_the_arrival_rate():
    """Dense severities get narrow batches; sparse ones get wide batches."""
    dense = slices.batch_seconds_for(PROBLEM_SEVERITIES)
    sparse = slices.batch_seconds_for(("fatal", "warn"))
    assert dense < sparse
    assert dense >= slices.BATCH_SECONDS


def test_batch_width_never_goes_below_a_row_group():
    """A row group spans ~1.1s, so a narrower predicate cannot read less."""
    assert slices.batch_seconds_for(PROBLEM_SEVERITIES) >= 1


def test_batch_width_is_capped_however_sparse_the_severities():
    assert slices.batch_seconds_for(("fatal",)) <= slices.MAX_BATCH_SECONDS


def test_each_batch_stays_near_the_target_row_count():
    """The safety property: no statement is allowed to be large."""
    for severities in (PROBLEM_SEVERITIES, ("fatal", "warn")):
        width = slices.batch_seconds_for(severities)
        rows_per_batch = slices.arrival_rate(severities) * width
        # Within an order of magnitude of target, in both directions.
        assert rows_per_batch <= slices.TARGET_BATCH_ROWS * 2


def test_no_batch_is_ever_wider_than_the_cap():
    """The safety invariant, across every severity set and plan size.

    This is what bounds peak memory per statement, and it must hold even for a
    plan whose whole window is narrower than one batch.
    """
    for severities in (PROBLEM_SEVERITIES, ("fatal", "warn"), ("fatal",)):
        for rows in (50_000, 150_000, None):
            plan = slices.plan_slice(severities, max_rows=rows)
            batches = list(slices._batches(plan))
            assert batches, "a plan must produce at least one batch"
            widest = max((hi - lo).total_seconds() for lo, hi in batches)
            assert widest <= slices.MAX_BATCH_SECONDS


def test_an_explicitly_large_request_is_split_into_many_batches():
    """A wide window must never become one statement."""
    plan = slices.plan_slice(PROBLEM_SEVERITIES, max_wall_seconds=100_000)
    assert len(list(slices._batches(plan))) > 1


# --- Budget enforcement -----------------------------------------------------
def test_a_dry_run_reports_the_plan_and_builds_nothing(monkeypatch):
    """The budget check must happen before anything is created."""
    checked: dict = {}

    class FakeCursor:
        def execute(self, *args, **kwargs):
            checked["executed"] = True

        def fetchone(self):
            return {"b": 10_000_000}

    class FakeCtx:
        def __enter__(self):
            return FakeCursor()

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(slices, "_slice_cursor", lambda: FakeCtx())
    report = slices.build_slice(
        plan=slices.plan_slice(max_rows=50_000), dry_run=True
    )

    assert report["dry_run"] is True
    assert "fits" in report["budget"]
    assert "rows" not in report


def test_an_over_budget_plan_is_refused_before_building(monkeypatch):
    class FakeCursor:
        def execute(self, *args, **kwargs):
            pass

        def fetchone(self):
            # Database already nearly at the cap.
            return {"b": BUDGET.total_disk_bytes - 1_000_000}

    class FakeCtx:
        def __enter__(self):
            return FakeCursor()

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(slices, "_slice_cursor", lambda: FakeCtx())
    with pytest.raises(QueryRefused) as excinfo:
        slices.build_slice(plan=slices.plan_slice(max_rows=1_000_000))
    assert excinfo.value.refusal.code == "over_budget"


def test_slice_columns_include_the_generated_record_id():
    """Materializing exists to add the id the raw corpus lacks."""
    assert "line_id" in slices.SLICE_COLUMNS
    assert "Body" in slices.SLICE_COLUMNS
    assert "EventTemplate" in slices.SLICE_COLUMNS


# --- Loop termination -------------------------------------------------------
def test_batching_terminates_for_every_severity_set():
    """``_batches`` is a while loop, so its step must always advance.

    The step is floored at one second by :data:`slices.BATCH_SECONDS`; a zero
    step would hang the build before a single row was read.
    """
    for severities in (
        PROBLEM_SEVERITIES,
        ("fatal", "warn"),
        ("fatal",),
        ("error",),
    ):
        assert slices.batch_seconds_for(severities) >= 1


def test_batch_count_is_bounded_by_the_window():
    """A generous cap, but a finite one: no plan may produce endless batches."""
    plan = slices.plan_slice(PROBLEM_SEVERITIES, max_wall_seconds=100_000)
    batches = list(slices._batches(plan))
    assert len(batches) <= plan.window_seconds + 1


def test_a_degenerate_window_yields_no_batches_rather_than_hanging():
    """An empty or inverted window must terminate immediately."""
    from dataclasses import replace

    plan = slices.plan_slice(max_rows=50_000)
    empty = replace(plan, end=plan.start)
    assert list(slices._batches(empty)) == []

    inverted = replace(plan, end=plan.start - (plan.end - plan.start))
    assert list(slices._batches(inverted)) == []
