"""Budget arithmetic and the measured corpus facts.

Offline: every number here is a constant or pure arithmetic over one.
"""

from __future__ import annotations

import pytest

from backend.db.budget import (
    BUDGET,
    BUILD_ROWS_PER_SECOND,
    CHUNK_ROWS,
    CORPUS_FACTS,
    GIB,
    INDEX_BYTES_PER_ROW,
    MATERIALIZED_BYTES_PER_ROW,
    MAX_PROMOTION_BUILD_SECONDS,
    MAX_SLICE_BUILD_SECONDS,
    MIB,
    OTEL_SLICE_MAX_ROWS,
    PROBLEM_SEVERITIES,
    PROMOTION_MAX_ROWS,
    estimate_build_seconds,
    estimate_index_bytes,
    fits,
    max_buildable_rows,
    max_indexable_rows,
)


# --- Measured corpus facts ---------------------------------------------------
def test_part_file_is_one_billion_rows():
    assert CORPUS_FACTS.rows_per_file == 1_000_000_000
    assert CORPUS_FACTS.row_groups_per_file == 1_624


def test_compressed_bytes_per_row_is_about_forty_one():
    # 40,991,166,725 bytes / 1e9 rows, measured.
    assert 40.0 < CORPUS_FACTS.compressed_bytes_per_row() < 42.0


def test_row_group_is_about_six_hundred_thousand_rows():
    # The pruning unit: ~614k rows spanning ~1.1s. A time filter narrower than
    # a second cannot read less than this.
    assert 600_000 <= CORPUS_FACTS.rows_per_row_group <= 625_000
    assert CORPUS_FACTS.seconds_per_row_group < 2.0


def test_severity_values_are_lower_case():
    # Measured: the corpus uses lower case. Filtering on 'ERROR' matches nothing.
    for name in CORPUS_FACTS.severity_distribution:
        assert name == name.lower(), name
    for name in PROBLEM_SEVERITIES:
        assert name == name.lower(), name


def test_severity_distribution_sums_to_about_one():
    assert 0.99 <= sum(CORPUS_FACTS.severity_distribution.values()) <= 1.01


def test_error_rows_vastly_exceed_the_index_budget():
    """The finding that reshaped the indexed slice.

    A severity filter alone does not carve out an indexable slice: ``error`` is
    ~13.8% of a billion rows. The slice must also be time-bounded.
    """
    error_rows = CORPUS_FACTS.rows_in_severities(PROBLEM_SEVERITIES)
    assert error_rows > 100_000_000
    assert error_rows > 10 * max_buildable_rows(
        BUDGET.otel_slice_allowance_bytes, MAX_SLICE_BUILD_SECONDS
    )


# --- Dual cost model (measured) ---------------------------------------------
def test_view_backed_index_is_cheaper_than_materializing():
    """Measured: 73-74 B/row through a view, 293 B/row materialized.

    The 4x gap is why promotion indexes a view instead of doing the CTAS the
    plan assumed it would need.
    """
    assert INDEX_BYTES_PER_ROW < MATERIALIZED_BYTES_PER_ROW
    assert estimate_index_bytes(1000) < estimate_index_bytes(1000, materialized=True)


def test_per_row_costs_match_what_was_measured():
    """Both figures come from completed builds, not extrapolation.

    The materialized cost was revised down from 300 to 155: the original came
    from a 100k-row CTAS probe (226 B table + 67 B index), but a real
    150,557-row slice build measured 4.19 MB of table and 19.12 MB of index --
    154.9 B/row, about half the probe's figure, because the probe was too small
    for compression and index structure to amortise.
    """
    assert 70 <= INDEX_BYTES_PER_ROW <= 90
    assert 140 <= MATERIALIZED_BYTES_PER_ROW <= 170


def test_build_throughput_uses_the_conservative_measurement():
    # Measured 16,000 rows/s at 156k rows but only 6,300 rows/s at 687k.
    # Throughput degrades, so the figure must be the worse one.
    assert BUILD_ROWS_PER_SECOND <= 6_500


def test_estimate_build_seconds_is_linear_and_positive():
    assert estimate_build_seconds(0) == 0
    assert estimate_build_seconds(12_000) == pytest.approx(
        2 * estimate_build_seconds(6_000)
    )


# --- Budget consistency ------------------------------------------------------
def test_budget_allowances_fit_inside_the_disk_cap():
    assert BUDGET.total_disk_bytes == 3 * GIB
    assert BUDGET.is_self_consistent()
    assert BUDGET.allocated_bytes() <= BUDGET.total_disk_bytes


def test_memory_limit_leaves_headroom_under_six_gigabytes():
    # The server's own default of 5.7 GiB is what a 2.44M-row CTAS died at, so
    # the session limit sits below it.
    assert BUDGET.memory_limit_bytes <= 6 * GIB
    assert BUDGET.memory_limit_bytes < int(5.7 * GIB)


def test_headroom_is_reserved():
    assert BUDGET.headroom_bytes >= 256 * MIB


# --- Sizing ------------------------------------------------------------------
def test_estimate_index_bytes_is_linear_in_rows():
    assert estimate_index_bytes(0) == 0
    assert estimate_index_bytes(2_000) == 2 * estimate_index_bytes(1_000)


def test_max_indexable_rows_inverts_the_estimate():
    rows = max_indexable_rows(BUDGET.otel_slice_allowance_bytes)
    assert estimate_index_bytes(rows) <= BUDGET.otel_slice_allowance_bytes
    assert estimate_index_bytes(rows + 1) > BUDGET.otel_slice_allowance_bytes


def test_build_time_binds_before_disk_for_the_slice():
    """The constraint the original plan missed.

    Disk would permit millions of rows; the measured 6,000 rows/s means a
    300-second build reaches far fewer. Sizing must take the smaller of the two.

    The margin narrowed when the slice allowance was cut from 1 GiB to 256 MiB,
    but the ordering is the point, not the magnitude -- and 1 GiB was dead
    allocation the slice could never reach anyway.
    """
    disk_limited = max_indexable_rows(BUDGET.otel_slice_allowance_bytes)
    both_limited = max_buildable_rows(
        BUDGET.otel_slice_allowance_bytes, MAX_SLICE_BUILD_SECONDS
    )
    assert disk_limited > 1_000_000
    assert both_limited < disk_limited
    assert estimate_build_seconds(both_limited) <= MAX_SLICE_BUILD_SECONDS


def test_slice_and_chunk_sizes_are_derived_not_hardcoded():
    assert OTEL_SLICE_MAX_ROWS == max_buildable_rows(
        BUDGET.otel_slice_allowance_bytes, MAX_SLICE_BUILD_SECONDS
    )
    assert estimate_index_bytes(OTEL_SLICE_MAX_ROWS) <= (
        BUDGET.otel_slice_allowance_bytes
    )


def test_chunk_rows_respects_the_crash_derived_ceiling():
    """Chunk size is derived, and the OOM-derived ceiling binds.

    A 2.44M-row materialization killed the server, so one promoted chunk stays
    an order of magnitude below that regardless of what disk or time allow.
    """
    assert CHUNK_ROWS <= PROMOTION_MAX_ROWS
    assert CHUNK_ROWS < 2_440_000 / 5
    assert estimate_index_bytes(CHUNK_ROWS) <= BUDGET.promotion_allowance_bytes
    assert estimate_build_seconds(CHUNK_ROWS) <= MAX_PROMOTION_BUILD_SECONDS


# --- fits() ------------------------------------------------------------------
def test_fits_accepts_a_build_inside_its_allowance():
    verdict = fits(10 * MIB, dataset_id="openstack")
    assert verdict.ok
    assert verdict.allowance_bytes == BUDGET.openstack_allowance_bytes


def test_fits_refuses_a_build_over_its_allowance_with_a_row_suggestion():
    verdict = fits(5 * GIB, dataset_id="otel_s3")
    assert not verdict.ok
    assert "exceeds" in verdict.reason
    assert "rows" in verdict.suggestion

    refusal = verdict.as_refusal()
    assert refusal["status"] == "refused"
    assert refusal["estimated_bytes"] == 5 * GIB
    assert refusal["suggestion"]


def test_fits_refuses_when_the_total_cap_would_be_passed():
    """The per-dataset allowance can pass while the total cap fails.

    Uses a request comfortably inside its own allowance, so the only thing that
    can refuse it is the database-wide cap.
    """
    request = 32 * MIB
    assert request < BUDGET.openstack_allowance_bytes

    verdict = fits(
        request,
        dataset_id="openstack",
        current_db_bytes=BUDGET.total_disk_bytes - 8 * MIB,
    )
    assert not verdict.ok
    assert "cap" in verdict.reason
    assert "evict" in verdict.suggestion.lower()


def test_fits_skips_the_total_check_when_db_size_is_unknown():
    assert fits(10 * MIB, dataset_id="openstack", current_db_bytes=None).ok


def test_unknown_dataset_falls_back_to_the_promotion_allowance():
    verdict = fits(1, dataset_id="not_registered")
    assert verdict.allowance_bytes == BUDGET.promotion_allowance_bytes


@pytest.mark.parametrize("dataset_id", ["openstack", "bgl", "otel_slice", "otel_chunk"])
def test_every_buildable_dataset_has_a_positive_allowance(dataset_id):
    verdict = fits(1, dataset_id=dataset_id)
    assert verdict.allowance_bytes > 0
    assert verdict.ok


def test_remote_tier_may_never_build_anything_locally():
    """``otel_s3`` is read in place, so its disk allowance is zero.

    Any attempt to build an index or materialize rows against the remote tier is
    refused by construction rather than by a reviewer noticing.
    """
    verdict = fits(1, dataset_id="otel_s3")
    assert verdict.allowance_bytes == 0
    assert not verdict.ok
