"""BGL registration: proof the dataset registry is an abstraction.

The claim under test is narrow and falsifiable: **adding a third corpus required
no new SQL in any query builder.** If it had, the registry would be a pair of
special cases wearing an interface.

BGL was chosen because it disagrees with the other two corpora in every way that
could have been accidentally hard-coded:

* severities are UPPERCASE (``INFO``, ``FATAL``, ``SEVERE``) where OTel's are
  lowercase, so a global severity vocabulary would be wrong for one of them;
* its timestamp arrives as Unix epoch seconds, beside a ``Time`` column holding
  ``2005-06-03-15.42.50.675872``, which no database will parse;
* ``Node`` is high cardinality (1,778 distinct values in 2,000 rows), so it
  behaves like a trace id rather than a service;
* it spans seven months, where the others span a day or half an hour.

That last property exposed a real gap: at ``day`` granularity a full-range BGL
trend needs 245 buckets and was refused, leaving a corpus whose natural
questions are long-range unable to answer any of them. ``week`` and ``month``
buckets were added in response.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from sql_render import normalized  # noqa: E402

import backend.db as db  # noqa: E402
from backend.db import loghub  # noqa: E402
from backend.db.datasets import Capability, dataset_ids, resolve  # noqa: E402
from backend.db.errors import QueryRefused  # noqa: E402
from backend.db.predicates import BUCKETS  # noqa: E402

WINDOW = ("2005-06-01 00:00:00+00", "2006-02-01 00:00:00+00")


def build(fn, *args, **kwargs):
    return fn.__wrapped__(*args, **kwargs)


# --- Registration -----------------------------------------------------------
def test_bgl_is_registered():
    assert "bgl" in dataset_ids()


def test_bgl_declares_what_it_can_do():
    bgl = resolve("bgl")
    for capability in (
        Capability.BM25,
        Capability.RECORD_ID,
        Capability.EXACT_COUNTS,
        Capability.TEMPLATES,
        Capability.AUTO_COMPLETE,
    ):
        assert bgl.supports(capability)


def test_bgl_does_not_claim_per_row_embeddings():
    """Semantic search comes from template embeddings, not per-row vectors."""
    assert not resolve("bgl").supports(Capability.VECTOR)


def test_bgl_is_not_chunk_sweepable():
    """The sweep is for the remote corpus; BGL is 2,000 local rows."""
    assert not resolve("bgl").supports(Capability.CHUNK_SWEEP)


def test_bgl_maps_every_canonical_field():
    """A corpus missing a canonical field would force callers to special-case."""
    bgl = resolve("bgl")
    for name in (
        "ts",
        "severity",
        "service",
        "message",
        "entity_id",
        "template",
        "event_id",
        "record_id",
    ):
        assert bgl.sql_for(name) is not None


# --- The vocabulary each corpus owns ----------------------------------------
def test_severity_vocabulary_is_per_dataset_and_uppercase_for_bgl():
    assert resolve("bgl").severity_values == (
        "INFO",
        "WARNING",
        "ERROR",
        "SEVERE",
        "FATAL",
    )


def test_the_three_corpora_disagree_about_severity_case():
    """The reason a global severity constant could not work."""
    bgl = set(resolve("bgl").severity_values)
    otel = set(resolve("otel_s3").severity_values)
    assert bgl & otel == set()
    assert all(s.isupper() for s in bgl)
    assert all(s.islower() for s in otel)


def test_a_lowercase_severity_is_refused_for_bgl():
    """And the refusal has to name the correction for *this* corpus."""
    with pytest.raises(QueryRefused) as excinfo:
        build(db.count_by_field, *WINDOW, dataset="bgl", severity="fatal")
    refusal = excinfo.value.refusal
    assert refusal.code == "severity_case"
    assert "bgl" in refusal.reason


def test_an_uppercase_severity_is_refused_for_otel():
    """The same guard, inverted, so neither corpus is the privileged one."""
    with pytest.raises(QueryRefused) as excinfo:
        build(
            db.count_by_field,
            "2025-09-23 00:00:00+00",
            "2025-09-23 00:00:10+00",
            dataset="otel_s3",
            severity="ERROR",
        )
    assert excinfo.value.refusal.code == "severity_case"


# --- Zero new SQL: the builders are unchanged -------------------------------
def test_every_builder_composes_against_bgl():
    """The central property. Each builder must produce SQL naming BGL's own
    columns, without any BGL-specific branch existing in it."""
    cases = (
        (db.count_by_field, WINDOW, {"dataset": "bgl"}),
        (db.count_distinct_by_field, WINDOW, {"dataset": "bgl", "field": "service"}),
        (db.top_by_field, WINDOW, {"dataset": "bgl", "field": "service"}),
        (db.get_field_values, (*WINDOW, "template"), {"dataset": "bgl"}),
        (
            db.get_trend,
            (*WINDOW, "service", "KERNEL"),
            {"dataset": "bgl", "bucket": "month"},
        ),
        (db.search_logs, ("parity error",), {"dataset": "bgl"}),
        (db.get_log_by_record_id, (1,), {"dataset": "bgl"}),
    )
    for fn, args, kwargs in cases:
        rendered = normalized(build(fn, *args, **kwargs)[0])
        assert '"bgl"' in rendered or "bgl_" in rendered, fn.__name__


def test_builders_reference_bgl_columns_not_openstack_ones():
    """No other corpus's column names may leak into BGL's SQL."""
    severity_only = normalized(build(db.count_by_field, *WINDOW, dataset="bgl")[0])
    assert '"Level"' in severity_only
    assert '"bgl"' in severity_only
    # OpenStack's severity and timestamp columns must not appear.
    assert "category" not in severity_only
    assert "log_timestamp" not in severity_only

    by_service = normalized(
        build(db.top_by_field, *WINDOW, dataset="bgl", field="service")[0]
    )
    assert '"Component"' in by_service
    assert '"ServiceName"' not in by_service


def test_search_uses_the_bgl_index_relation():
    rendered = normalized(build(db.search_logs, "parity", dataset="bgl")[0])
    assert "bgl_idx" in rendered
    assert "read_parquet" not in rendered


def test_bgl_timestamps_are_cast_as_timestamptz():
    """Epoch seconds are converted at load time, so queries see a real
    timestamptz and the generic ts filter applies unchanged."""
    assert resolve("bgl").ts_cast == "timestamptz"


# --- Long-range buckets -----------------------------------------------------
def test_coarse_buckets_exist_for_corpora_spanning_months():
    assert "week" in BUCKETS
    assert "month" in BUCKETS


def test_a_seven_month_window_is_refused_at_day_granularity():
    """Not a defect -- the result-size cap doing its job."""
    with pytest.raises(QueryRefused) as excinfo:
        build(db.get_trend, *WINDOW, "service", "KERNEL", dataset="bgl", bucket="day")
    assert excinfo.value.refusal.code == "too_many_buckets"


def test_the_refusal_suggests_a_coarser_bucket_that_actually_exists():
    """A suggestion naming an unsupported bucket would be a dead end."""
    with pytest.raises(QueryRefused) as excinfo:
        build(db.get_trend, *WINDOW, "service", "KERNEL", dataset="bgl", bucket="day")
    suggestion = excinfo.value.refusal.suggestion
    assert any(b in suggestion for b in BUCKETS)


def test_month_buckets_compose_for_the_full_bgl_range():
    rendered = normalized(
        build(
            db.get_trend, *WINDOW, "service", "KERNEL", dataset="bgl", bucket="month"
        )[0]
    )
    assert "month" in rendered


# --- Loader -----------------------------------------------------------------
def test_the_loader_spec_is_registered_for_bgl():
    spec = loghub.spec_for("bgl")
    assert spec.dataset_id == "bgl"
    assert spec.csv_name.endswith("_structured.csv")


def test_an_unknown_corpus_is_refused_with_the_loadable_list():
    with pytest.raises(QueryRefused) as excinfo:
        loghub.spec_for("hdfs")
    assert excinfo.value.refusal.code == "unknown_dataset"
    assert "bgl" in excinfo.value.refusal.suggestion


def test_the_loader_derives_a_real_timestamp_from_epoch_seconds():
    """loghub's own ``Time`` string is unparseable, so the epoch column is the
    only usable source."""
    spec = loghub.spec_for("bgl")
    derived = dict(spec.derived)
    assert "Timestamp" in derived
    assert "EpochSeconds" in derived["Timestamp"]


def test_derived_expressions_quote_their_identifiers():
    """Unquoted names fold to lower case and fail against CamelCase columns."""
    for _, expression in loghub.spec_for("bgl").derived:
        assert '"' in expression


def test_every_selector_column_is_in_the_token_index():
    """The auto-complete recipe matches through the token relation, so a
    selector column missing from it fails at query time rather than at load."""
    bgl = resolve("bgl")
    spec = loghub.spec_for("bgl")
    token_columns = {name for name, _ in spec.token_columns}

    for char, field_name in bgl.selectors.items():
        if field_name == "message":
            continue  # matched through the verbatim index, not the token one
        column = bgl.fields[field_name].column
        assert column in token_columns, f"selector {char} -> {column}"


def test_the_loader_names_indexes_from_the_registry():
    """The registry stays the single source of truth for what a query reads."""
    bgl = resolve("bgl")
    assert bgl.index_name == "bgl_idx"
    assert bgl.token_index_name == "bgl_token_idx"
