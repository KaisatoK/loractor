"""Offline shape tests for the trend and period-comparison query builders.

Both are ``query_runner`` functions (they execute SQL), so these tests call the
undecorated builders via ``__wrapped__``; no database is touched. They target the
registry-driven builders: the corpus is chosen by ``dataset`` id and columns come
from its canonical-field mapping, so the same test shape covers OpenStack and BGL.
"""

import pytest

from backend.db import count_query, trend_query
from sql_render import normalized

LO = "2017-05-16 00:00:00"
HI = "2017-05-16 03:00:00"

TREND_CASES = [
    ("openstack", "entity_id", 2931, '"Pid" = %s'),
    ("openstack", "service", "nova.compute.manager", '"Component" = %s'),
    ("bgl", "service", "KERNEL", '"Component" = %s'),
]


@pytest.mark.parametrize(("dataset", "field", "value", "filter_sql"), TREND_CASES)
def test_trend_buckets_by_full_truncated_timestamp(dataset, field, value, filter_sql):
    query, params, _ = trend_query.get_trend.__wrapped__(
        LO, HI, field, value, dataset=dataset, bucket="minute"
    )
    text = normalized(query)

    # Minute-of-hour (0-59) merged buckets across hours; the full truncated
    # timestamp keeps every real minute separate and chronologically ordered.
    assert "date_trunc('minute', \"Timestamp\")" in text
    assert "EXTRACT(MINUTE" not in text.upper()
    assert filter_sql in text
    assert "ORDER BY bucket" in text
    assert '"Level" = %s' not in text
    assert value in params


@pytest.mark.parametrize(("dataset", "field", "value", "filter_sql"), TREND_CASES)
def test_trend_severity_filter_appends_param(dataset, field, value, filter_sql):
    severity = "ERROR"
    query, params, _ = trend_query.get_trend.__wrapped__(
        LO, HI, field, value, dataset=dataset, severity=severity
    )
    assert '"Level" = %s' in normalized(query)
    # The filter's parameters come first; the bucket grid's three bounds follow.
    assert params[-4] == severity
    assert params[-3:] == [LO, HI, HI]


def test_openstack_accepts_critical_as_a_severity():
    query, params, _ = trend_query.get_trend.__wrapped__(
        LO, HI, "service", "nova.compute.manager", dataset="openstack",
        severity="CRITICAL",
    )
    assert '"Level" = %s' in normalized(query)
    assert params[-4] == "CRITICAL"


# --- Zero-filled buckets, chosen bucket, per-severity series (T5) -------------
def test_every_bucket_in_the_window_is_returned_with_zero_for_empty_ones():
    query, params, stats = trend_query.get_trend.__wrapped__(
        LO, HI, "service", "nova.compute.manager", dataset="openstack", bucket="hour"
    )
    text = normalized(query)
    # Materialized, so the remote scan keeps its pruning (see trend_query).
    assert text.startswith("WITH bucket_counts AS MATERIALIZED (")
    # A generated grid, left-joined to the counts, with 0 where none matched.
    assert (
        "generate_series( date_trunc('hour', %s::timestamptz), %s::timestamptz, "
        "CAST('1 hour' AS INTERVAL) ) AS s(bucket)"
    ) in text
    assert "WHERE s.bucket < %s::timestamptz" in text
    assert "LEFT JOIN bucket_counts c ON c.bucket = g.bucket" in text
    assert "COALESCE(c.count, 0) AS count" in text
    # LAG runs over the filled series, so neighbours really are adjacent.
    assert "FROM filled" in text
    assert "LAG(count) OVER (ORDER BY bucket)" in text
    assert params[-3:] == [LO, HI, HI]
    assert any("0 where nothing was logged" in n for n in stats.notes)


@pytest.mark.parametrize(
    ("window_seconds", "series", "expected"),
    [
        (60, 1, "second"),
        (300, 1, "minute"),            # 300 seconds would be 300 points
        (3 * 3600, 1, "hour"),         # 180 minutes is too many
        (231_814, 1, "hour"),          # the whole OpenStack corpus: ~65 hours
        (231_814, 4, "day"),           # ...per severity: 4 x 65 is too many
        (18_551_835, 1, "week"),       # the whole BGL corpus: ~215 days
        (365 * 86_400, 6, "month"),
    ],
)
def test_the_default_bucket_keeps_the_series_within_the_default_points(
    window_seconds, series, expected
):
    assert trend_query.choose_bucket(window_seconds, series) == expected


def test_an_omitted_bucket_is_chosen_from_the_window_and_reported():
    query, _, stats = trend_query.get_trend.__wrapped__(
        LO, HI, "service", "nova.compute.manager", dataset="openstack"
    )
    assert "date_trunc('hour'" in normalized(query)
    assert any("hour (chosen from the window length)" in n for n in stats.notes)


def test_group_by_severity_gives_one_zero_filled_series_per_level_present():
    query, _, _ = trend_query.get_trend.__wrapped__(
        LO, HI, "service", "nova.compute.manager", dataset="openstack",
        bucket="hour", group_by="severity",
    )
    text = normalized(query)
    assert '"Level" AS severity, count(*) AS count' in text
    assert 'GROUP BY CAST(date_trunc(\'hour\', "Timestamp") AS timestamptz), "Level"' in text
    # Levels come from the window's rows, never from a declared list.
    assert "CROSS JOIN (SELECT DISTINCT severity FROM bucket_counts) AS sv" in text
    assert "c.bucket = g.bucket AND c.severity = g.severity" in text
    assert "LAG(count) OVER (PARTITION BY severity ORDER BY bucket)" in text
    assert "ORDER BY bucket, severity" in text


def test_group_by_counts_every_series_against_the_row_cap():
    from backend.db.errors import QueryRefused

    # 65 hourly buckets is fine alone, but not for each of 4 OpenStack levels.
    with pytest.raises(QueryRefused) as refused:
        trend_query.get_trend.__wrapped__(
            "2017-05-14 19:39:01", "2017-05-17 12:02:36", "service", "x",
            dataset="openstack", bucket="hour", group_by="severity",
        )
    assert refused.value.refusal.code == "too_many_buckets"
    assert "day" in refused.value.refusal.suggestion


def test_an_unknown_group_by_is_refused():
    from backend.db.errors import QueryRefused

    with pytest.raises(QueryRefused) as refused:
        trend_query.get_trend.__wrapped__(
            LO, HI, "service", "x", dataset="openstack", group_by="service"
        )
    assert refused.value.refusal.code == "bad_group_by"


def test_the_schema_accepts_every_bucket_the_query_layer_supports():
    """A refusal suggesting 'week' must be followable through the schema."""
    from backend.db.predicates import BUCKETS
    from backend.pipeline.tools.schemas import GetTrend

    for bucket in BUCKETS:
        args = GetTrend(
            start_time=LO, end_time=HI, type=("service", "x"), bucket=bucket
        )
        assert args.bucket == bucket
    assert GetTrend(start_time=LO, end_time=HI, type=("service", "x")).bucket is None


def test_compare_periods_groups_by_field_and_severity():
    query, params, stats = count_query.compare_periods.__wrapped__(
        "2017-05-14 00:00:00", "2017-05-15 00:00:00", LO, HI, dataset="openstack",
    )
    text = normalized(query)

    assert '"Component" AS grp' in text
    assert '"Level" AS sev' in text
    assert "FULL OUTER JOIN baseline" in text
    for column in ("baseline_count", "recent_count", "volume_change", "percentage_change"):
        assert column in text
    assert stats.dataset == "openstack"
    assert len(params) == 4
