"""Sampling uncertainty, and indexing the corpus in place.

Two things a sweep must be honest about, and one architectural consequence.

**A sample is a guess.** Stage 3 reads ~0.3% of a chunk as a handful of windows
of consecutive rows. That is a deliberate, quicksort-pivot sort of choice --
cheap, and making no claim to be representative -- but it means a sampled rate
carries real uncertainty, and the raw ratio understates it twice over: the rows
are clustered, so 2,000 of them from 8 windows carry about 8 independent
observations, and a pattern living in the unsampled 99.7% is simply invisible.

**The corpus is not downloaded.** An inverted index is built directly over the
remote Parquet, so rows stay in object storage and only the postings are local.
Measured against materializing the same two row groups: 464 s against 1,324 s to
build, 60.1 B/row against 154.9, and nothing copied.
"""

from __future__ import annotations

import pytest

from backend.db import external
from backend.db.errors import QueryRefused
from backend.pipeline.triage import SampleUncertainty, sample_uncertainty


def rows_for(window_rates: list[float], per_window: int = 100) -> list[dict]:
    """Sample rows whose per-window match rates are exactly as given."""
    rows: list[dict] = []
    for window, rate in enumerate(window_rates):
        hits = int(round(rate * per_window))
        for i in range(per_window):
            rows.append(
                {
                    "sample_window": window,
                    "message": "failed to charge card" if i < hits else "request ok",
                }
            )
    return rows


# --- Uncertainty is measured between windows, not across rows ---------------
def test_agreeing_windows_give_a_tight_range():
    u = sample_uncertainty(
        rows_for([0.6] * 8), ["charge"], rows_in_chunk=614_400
    )
    low, high = u.interval
    assert u.hit_rate == pytest.approx(0.6, abs=0.01)
    assert high - low < 0.05
    assert u.is_decisive


def test_disagreeing_windows_give_a_wide_range_at_the_same_rate():
    """The case a pooled ratio hides.

    Both samples pool to ~60%, but in one the chunk is uniform and in the other
    five windows are full of matches and three have none. Quoting "60%" for the
    second would be a fabricated precision.
    """
    uniform = sample_uncertainty(
        rows_for([0.6] * 8), ["charge"], rows_in_chunk=614_400
    )
    lumpy = sample_uncertainty(
        rows_for([1.0, 1.0, 1.0, 1.0, 1.0, 0.0, 0.0, 0.0]),
        ["charge"],
        rows_in_chunk=614_400,
    )

    assert lumpy.hit_rate == pytest.approx(uniform.hit_rate, abs=0.05)
    assert (lumpy.interval[1] - lumpy.interval[0]) > (
        uniform.interval[1] - uniform.interval[0]
    )
    assert not lumpy.is_decisive


def test_the_interval_is_clamped_to_real_proportions():
    u = sample_uncertainty(
        rows_for([1.0, 0.0] * 4), ["charge"], rows_in_chunk=614_400
    )
    low, high = u.interval
    assert 0.0 <= low <= high <= 1.0


def test_a_single_window_claims_no_precision():
    """With one cluster there is nothing to compare, so no interval is implied."""
    u = sample_uncertainty(rows_for([0.5]), ["charge"], rows_in_chunk=614_400)
    assert u.windows == 1
    assert u.standard_error == 0.0


def test_the_sampled_fraction_is_reported_honestly():
    u = sample_uncertainty(
        rows_for([0.5] * 8, per_window=250), ["charge"], rows_in_chunk=614_400
    )
    assert u.rows_sampled == 2_000
    assert u.sampled_fraction == pytest.approx(2_000 / 614_400, rel=1e-6)
    assert u.sampled_fraction < 0.004


def test_no_matches_is_not_decisive():
    """Absence in 0.3% of a chunk is not evidence of absence in the chunk."""
    u = sample_uncertainty(rows_for([0.0] * 8), ["charge"], rows_in_chunk=614_400)
    assert u.hit_rate == 0.0
    assert not u.is_decisive


# --- The caveat an answer has to carry --------------------------------------
def test_the_caveat_states_the_fraction_and_the_range():
    caveat = sample_uncertainty(
        rows_for([0.6] * 8, per_window=250), ["charge"], rows_in_chunk=614_400
    ).caveat()

    assert "2,000 of 614,400" in caveat
    assert "0.33%" in caveat
    assert "8 spread windows" in caveat


def test_the_caveat_explains_why_the_row_count_overstates_the_sample():
    caveat = sample_uncertainty(
        rows_for([0.6] * 8, per_window=250), ["charge"], rows_in_chunk=614_400
    ).caveat()
    assert "correlated" in caveat
    assert "8 independent observations" in caveat


def test_the_caveat_says_so_when_the_windows_disagree():
    caveat = sample_uncertainty(
        rows_for([1.0, 1.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0]),
        ["charge"],
        rows_in_chunk=614_400,
    ).caveat()
    assert "indicative only" in caveat


def test_uncertainty_serializes_for_the_tool_result():
    payload = sample_uncertainty(
        rows_for([0.4] * 8), ["charge"], rows_in_chunk=614_400
    ).as_dict()

    for key in (
        "rows_sampled",
        "rows_in_chunk",
        "sampled_fraction",
        "windows",
        "hit_rate",
        "hit_rate_interval",
        "decisive",
        "caveat",
    ):
        assert key in payload


# --- Rows are tagged so the clustering is recoverable -----------------------
def test_the_sample_query_tags_every_window():
    """Without the tag, uncertainty could only be computed across rows, which
    would report a tight interval around a number one window may have driven."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).parent))
    from sql_render import normalized

    from backend.db import chunks as chunk_mod
    from backend.db import pruning
    from backend.db.datasets import resolve
    from backend.db.profile import SAMPLE_WINDOWS, sample_chunk

    url = "https://example.invalid/part_000.parquet"
    pruning.clear_cache()
    pruning.seed_cache(url, pruning.synthetic_row_groups(url, 3))
    chunk_mod.clear_plan_cache()

    chunk = chunk_mod.plan_chunks(resolve("otel_s3"))[0]
    rendered = normalized(sample_chunk.__wrapped__(chunk, dataset="otel_s3")[0])

    assert rendered.count("AS sample_window") == SAMPLE_WINDOWS

    pruning.clear_cache()
    chunk_mod.clear_plan_cache()


# --- Indexing in place rather than downloading ------------------------------
def test_the_external_plan_copies_nothing():
    plan = external.plan_external_index(
        "2025-09-23 00:00:00+00", "2025-09-23 00:00:02+00"
    )
    assert plan.as_dict()["copies_data"] is False


def test_the_external_build_cost_is_flat_not_per_row_group():
    """Measured: 1, 2, 4 and 15 row groups all build in ~466 s.

    The cost is a fixed per-build scan that parallelises across groups, so
    widening the window is nearly free -- the opposite of the materialized path,
    which pays ~660 s per row group.
    """
    narrow = external.plan_external_index(
        "2025-09-23 00:00:00+00", "2025-09-23 00:00:02+00", max_seconds=4_000
    )
    wide = external.plan_external_index(
        "2025-09-23 00:00:00+00", "2025-09-23 00:00:16+00", max_seconds=4_000
    )

    assert wide.row_groups > narrow.row_groups * 5
    assert wide.estimated_seconds == pytest.approx(narrow.estimated_seconds)
    assert wide.estimated_rows > narrow.estimated_rows * 5


def test_beyond_the_measured_range_the_estimate_stops_claiming_flatness():
    """Flat to 15 row groups is measured; past that it is extrapolation."""
    measured = external.plan_external_index(
        "2025-09-23 00:00:00+00", "2025-09-23 00:00:16+00", max_seconds=10_000
    )
    beyond = external.plan_external_index(
        "2025-09-23 00:00:00+00", "2025-09-23 00:00:48+00", max_seconds=10_000
    )
    assert beyond.estimated_seconds > measured.estimated_seconds


def test_indexing_in_place_is_cheaper_than_materializing():
    """Both sides measured. Disk per row, and time for a comparable extract."""
    from backend.db.budget import MATERIALIZED_BYTES_PER_ROW
    from backend.db.slices import BODY_SECONDS_PER_ROW_GROUP

    assert external.EXTERNAL_BYTES_PER_ROW < MATERIALIZED_BYTES_PER_ROW

    # One external build covers the whole measured-flat range; materializing
    # the same span pays per row group.
    materialized = (
        external.MEASURED_FLAT_ROW_GROUPS * BODY_SECONDS_PER_ROW_GROUP
    )
    assert external.EXTERNAL_BUILD_SECONDS < materialized / 10


def test_an_unaffordable_window_is_refused_with_an_affordable_one():
    with pytest.raises(QueryRefused) as excinfo:
        external.plan_external_index(
            "2025-09-23 00:00:00+00", "2025-09-23 00:05:00+00"
        )
    refusal = excinfo.value.refusal
    assert refusal.code == "build_too_long"
    assert "row group" in refusal.suggestion


def test_an_inverted_window_is_refused():
    with pytest.raises(QueryRefused) as excinfo:
        external.plan_external_index(
            "2025-09-23 00:00:02+00", "2025-09-23 00:00:00+00"
        )
    assert excinfo.value.refusal.code == "inverted_window"


def test_the_estimate_matched_the_measured_build():
    """464 s predicted, 464.0 s measured, over two row groups."""
    plan = external.plan_external_index(
        "2025-09-23 00:00:00+00", "2025-09-23 00:00:02+00"
    )
    assert plan.estimated_seconds == pytest.approx(464.0, rel=0.02)
