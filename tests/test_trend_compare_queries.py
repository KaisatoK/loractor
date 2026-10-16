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
        LO, HI, field, value, dataset=dataset
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
    assert params[-1] == severity


def test_openstack_accepts_critical_as_a_severity():
    query, params, _ = trend_query.get_trend.__wrapped__(
        LO, HI, "service", "nova.compute.manager", dataset="openstack",
        severity="CRITICAL",
    )
    assert '"Level" = %s' in normalized(query)
    assert params[-1] == "CRITICAL"


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
