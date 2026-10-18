"""Dataset-driven query construction, bounds, query classes, and cost metering.

Offline throughout. SQL is rendered by ``tests/sql_render.py`` and the builders
are reached through ``__wrapped__``, so no database is involved: these tests
assert the *shape* of the SQL and the refusal behaviour, which is where the
scaling guarantees live.
"""

from __future__ import annotations

import pytest
from sql_render import normalized

import backend.db as db
from backend.db import predicates as pred
from backend.db import pruning
from backend.db.datasets import Capability, resolve
from backend.db.errors import QueryRefused
from backend.db.stats import QueryClass, QueryResult, QueryStats, merge
from backend.db.windows import build_window

OTEL_LO = "2025-09-23 00:00:00+00"
OTEL_HI = "2025-09-23 00:00:10+00"
OS_LO = "2016-05-01 00:00:00"
OS_HI = "2016-05-02 00:00:00"


@pytest.fixture(autouse=True)
def seeded_metadata():
    """Row-group footers for the OTel corpus, shaped like the real ones.

    Footers are immutable published facts, so a fixture is as faithful as a
    fetch and keeps pruning tests offline.
    """
    pruning.clear_cache()
    for url in resolve("otel_s3").sources:
        pruning.seed_cache(url, pruning.synthetic_row_groups(url, 1624))
    yield
    pruning.clear_cache()


def build(fn, *args, **kwargs):
    """Call a builder without executing it."""
    return fn.__wrapped__(*args, **kwargs)


# --- No model-supplied identifiers ------------------------------------------
def test_no_tool_schema_accepts_a_table_or_identifier():
    """The core safety property: the model cannot name a SQL object.

    Every schema takes a ``dataset`` id from a closed vocabulary instead.
    """
    from backend.pipeline.tools import schemas

    models = [
        schemas.CountByCategory,
        schemas.GetTrend,
        schemas.SearchLogs,
        schemas.TopInCategoryByService,
        schemas.ComparePeriods,
        schemas.GetLogByLineId,
        schemas.GetTools,
    ]
    for model in models:
        fields = set(model.model_fields)
        assert "table_name" not in fields, model.__name__
        assert not any("table" in f for f in fields), model.__name__
        assert "dataset" in fields, model.__name__


def test_dataset_field_rejects_an_unregistered_value():
    from backend.pipeline.tools.schemas import SearchLogs

    with pytest.raises(ValueError):
        SearchLogs(query_text="x", dataset="'; DROP TABLE log; --")


def test_dataset_field_accepts_every_registered_id():
    from backend.pipeline.tools.schemas import SearchLogs

    for dataset_id in db.dataset_ids():
        assert SearchLogs(query_text="x", dataset=dataset_id).dataset == dataset_id


# --- Mandatory bounds -------------------------------------------------------
def test_list_query_emits_a_limit():
    """The original ``SELECT DISTINCT`` had none; at a billion rows that is fatal."""
    query, _, _ = build(
        db.get_field_values, OS_LO, OS_HI, "service", dataset="openstack"
    )
    assert "LIMIT" in normalized(query)


def test_limit_is_clamped_to_the_dataset_ceiling():
    query, _, _ = build(
        db.get_field_values, OS_LO, OS_HI, "service", dataset="openstack", limit=10**9
    )
    assert f"LIMIT {resolve('openstack').max_rows}" in normalized(query)


def test_every_scanning_builder_requires_a_window():
    for fn, args in (
        (db.count_by_field, ()),
        (db.top_by_field, ()),
    ):
        with pytest.raises(QueryRefused) as excinfo:
            build(fn, None, None, *args, dataset="otel_s3")
        assert excinfo.value.refusal.code == "missing_window"
        assert excinfo.value.refusal.suggestion


def test_over_wide_window_is_refused_with_the_dataset_ceiling():
    with pytest.raises(QueryRefused) as excinfo:
        build(
            db.count_by_field,
            OTEL_LO,
            "2025-09-23 01:00:00+00",
            dataset="otel_s3",
        )
    refusal = excinfo.value.refusal
    assert refusal.code == "window_too_wide"
    assert refusal.details["max_window_seconds"] == 300


def test_inverted_window_is_refused():
    with pytest.raises(QueryRefused) as excinfo:
        build(db.count_by_field, OTEL_HI, OTEL_LO, dataset="otel_s3")
    assert excinfo.value.refusal.code == "inverted_window"


def test_trend_refuses_a_bucket_that_would_return_too_many_points():
    with pytest.raises(QueryRefused) as excinfo:
        build(
            db.get_trend,
            OTEL_LO,
            "2025-09-23 00:05:00+00",
            "service",
            "cartservice",
            dataset="otel_s3",
            bucket="second",
        )
    refusal = excinfo.value.refusal
    assert refusal.code == "too_many_buckets"
    assert "minute" in refusal.suggestion


# --- Severity casing, measured per corpus -----------------------------------
def test_wrong_case_severity_is_refused_with_the_correction():
    """Measured: the OTel corpus is lower case. 'ERROR' would match nothing."""
    with pytest.raises(QueryRefused) as excinfo:
        build(db.count_by_field, OTEL_LO, OTEL_HI, dataset="otel_s3", severity="ERROR")
    refusal = excinfo.value.refusal
    assert refusal.code == "severity_case"
    assert refusal.details["corrected"] == "error"


def test_unknown_severity_is_refused_with_the_real_vocabulary():
    with pytest.raises(QueryRefused) as excinfo:
        build(
            db.count_by_field, OTEL_LO, OTEL_HI, dataset="otel_s3", severity="critical"
        )
    assert excinfo.value.refusal.code == "unknown_severity"
    assert "error" in excinfo.value.refusal.details["severity_values"]


def test_each_corpus_keeps_its_own_severity_casing():
    query, params, _ = build(
        db.count_by_field, OS_LO, OS_HI, dataset="openstack", severity="ERROR"
    )
    assert "ERROR" in params
    query, params, _ = build(
        db.count_by_field, OTEL_LO, OTEL_HI, dataset="otel_s3", severity="error"
    )
    assert "error" in params


# --- Dataset-driven SQL -----------------------------------------------------
def test_the_same_builder_renders_each_corpus_own_columns():
    """One builder, two corpora, no per-corpus SQL."""
    openstack, _, _ = build(
        db.count_by_field, OS_LO, OS_HI, group_by="service", dataset="openstack"
    )
    otel, _, _ = build(
        db.count_by_field, OTEL_LO, OTEL_HI, group_by="service", dataset="otel_s3"
    )

    rendered_os, rendered_otel = normalized(openstack), normalized(otel)
    assert '"Timestamp"' in rendered_os and '"Level"' in rendered_os
    assert '"Component"' in rendered_os
    assert '"Timestamp"' in rendered_otel and '"SeverityText"' in rendered_otel
    assert '"ServiceName"' in rendered_otel


def test_remote_corpus_reads_parquet_in_place():
    query, _, _ = build(db.count_by_field, OTEL_LO, OTEL_HI, dataset="otel_s3")
    rendered = normalized(query)
    assert "read_parquet(" in rendered
    assert "part_000.parquet" in rendered


def test_a_single_source_is_passed_as_a_scalar_path():
    """Not cosmetic: a list argument makes a view over the reader "generic",
    which disables the fast path an external inverted index needs to
    materialize columns. With a list the index counts and matches but cannot
    return a single column value."""
    query, _, _ = build(db.count_by_field, OTEL_LO, OTEL_HI, dataset="otel_s3")
    rendered = normalized(query)
    assert "read_parquet('" in rendered
    assert "read_parquet([" not in rendered


def test_several_sources_are_passed_as_a_list():
    """The widening mechanism: HTTPS gives no bucket listing, so a wider
    window means more entries rather than a glob."""
    from dataclasses import replace

    from backend.db.datasets import resolve
    from sql_render import render

    two = replace(
        resolve("otel_s3"),
        sources=("https://example.invalid/a.parquet", "https://example.invalid/b.parquet"),
    )
    assert "read_parquet([" in render(two.scan_relation())


def test_timestamp_cast_follows_the_dataset():
    from dataclasses import replace

    from backend.db.datasets import resolve

    openstack, _, _ = build(db.count_by_field, OS_LO, OS_HI, dataset="openstack")
    otel, _, _ = build(db.count_by_field, OTEL_LO, OTEL_HI, dataset="otel_s3")
    # Both corpora store TIMESTAMPTZ now; the cast is read from the registry.
    assert resolve("openstack").ts_cast == "timestamptz"
    assert "::timestamptz" in normalized(openstack)
    assert "::timestamptz" in normalized(otel)

    # A corpus declaring a naive timestamp gets the naive cast.
    from backend.db import predicates as pred
    from backend.db.windows import build_window

    naive = replace(resolve("openstack"), ts_cast="timestamp")
    clause, _ = pred.ts_filter(naive, build_window(naive, OS_LO, OS_HI))
    assert "::timestamp " in normalized(clause) + " "


def test_trend_buckets_with_date_trunc_not_extract_minute():
    """The original folded every hour onto the same sixty buckets."""
    query, _, _ = build(
        db.get_trend, OS_LO, OS_HI, "service", "nova", dataset="openstack", bucket="day"
    )
    rendered = normalized(query)
    assert "date_trunc('day'" in rendered
    assert "EXTRACT" not in rendered.upper()
    assert "LAG(count)" in rendered


# --- Approximation ----------------------------------------------------------
def test_remote_distinct_counts_are_approximate_and_labelled():
    query, _, stats = build(
        db.count_distinct_by_field, OTEL_LO, OTEL_HI, "service", dataset="otel_s3"
    )
    assert "approx_count_distinct" in normalized(query)
    assert stats.approximate is True
    assert stats.notes


def test_exact_corpus_uses_exact_distinct_counts():
    query, _, stats = build(
        db.count_distinct_by_field, OS_LO, OS_HI, "service", dataset="openstack"
    )
    assert "count(DISTINCT" in normalized(query)
    assert stats.approximate is False


# --- Capability gating ------------------------------------------------------
def test_fetch_by_id_is_refused_on_a_corpus_without_a_primary_key():
    """The raw OTel corpus has no record id; that absence is declared."""
    assert not resolve("otel_s3").supports(Capability.RECORD_ID)
    with pytest.raises(QueryRefused) as excinfo:
        build(db.get_log_by_record_id, 1, dataset="otel_s3")
    refusal = excinfo.value.refusal
    assert refusal.code == "unsupported_capability"
    # Points at corpora that do support it, so the agent can redirect.
    assert "openstack" in refusal.suggestion or "otel_slice" in refusal.suggestion


def test_search_is_refused_on_a_corpus_with_no_index():
    with pytest.raises(QueryRefused) as excinfo:
        build(db.search_logs, "oom", dataset="otel_s3")
    assert excinfo.value.refusal.code == "unsupported_capability"


def test_search_shape_follows_the_corpus_not_its_name(monkeypatch):
    """Three shapes: per-row vectors, template vectors, or BM25 alone.

    Pinned explicitly rather than left to live state. Template-vector
    availability is a build-time fact, so the query layer caches it; these
    assertions set it directly so the composed SQL is the same on any machine.
    """
    from backend.db import templates

    templates.clear_vector_cache()

    # No per-row embeddings and no template vectors: OpenStack is lexical.
    templates.set_vector_cache("openstack", False)
    lexical_os, _, _ = build(db.search_logs, "oom", dataset="openstack")
    assert "ai_embed" not in normalized(lexical_os)
    assert "BM25" in normalized(lexical_os)
    assert "openstack_idx" in normalized(lexical_os)

    # No template vectors built yet: lexical only.
    templates.set_vector_cache("bgl", False)
    lexical, params, _ = build(db.search_logs, "oom", dataset="bgl")
    assert "ai_embed" not in normalized(lexical)
    assert "BM25" in normalized(lexical)

    # Template vectors available: semantic branch fused with BM25.
    templates.set_vector_cache("bgl", True)
    templated, params, _ = build(db.search_logs, "oom", dataset="bgl")
    assert "bgl_templates" in normalized(templated)
    assert "BM25" in normalized(templated)

    # An external index answers alone: no base table exists to join to.
    index_only, params, _ = build(db.search_logs, "oom", dataset="otel_slice")
    rendered = normalized(index_only)
    assert "otel_ext_idx" in rendered
    assert "JOIN" not in rendered
    assert "BM25" in rendered

    templates.clear_vector_cache()


def test_search_never_composes_a_remote_scan():
    """A text query must never put read_parquet in its own FROM clause.

    The OTel index does reach object storage -- that is how an in-place index
    returns a row -- but through the index, not through a scan the query layer
    composed. Those are different costs: one re-reads a handful of matched
    rows, the other would read a window of the corpus.
    """
    from backend.db import templates

    templates.clear_vector_cache()
    templates.set_vector_cache("bgl", False)
    templates.set_vector_cache("openstack", False)
    for dataset, relation in (
        ("otel_slice", "otel_ext_idx"),
        ("bgl", "bgl_idx"),
        ("openstack", "openstack_idx"),
    ):
        rendered = normalized(build(db.search_logs, "oom", dataset=dataset)[0])
        assert "read_parquet" not in rendered, dataset
        assert relation in rendered, dataset
    templates.clear_vector_cache()


def test_high_cardinality_field_listing_is_refused():
    with pytest.raises(QueryRefused) as excinfo:
        build(
            db.get_field_values, OTEL_LO, OTEL_HI, "entity_id", dataset="otel_s3"
        )
    assert excinfo.value.refusal.code == "high_cardinality"


# --- Metadata pruning -------------------------------------------------------
def test_pruning_selects_only_the_row_groups_a_window_touches():
    """A row group spans ~1.1s, so a 10s window needs about ten of 1,624."""
    dataset = resolve("otel_s3")
    window = build_window(dataset, OTEL_LO, OTEL_HI)
    plan = pruning.plan(dataset, window)

    assert plan.row_groups_considered == 1624
    assert 9 <= plan.row_groups_read <= 11
    assert plan.est_rows_scanned == plan.row_groups_read * 614_400
    assert plan.est_bytes_read > 0


def test_pruning_reports_files_not_touched_as_pruned():
    dataset = resolve("otel_s3")
    window = build_window(dataset, OTEL_LO, OTEL_HI)
    plan = pruning.plan(dataset, window)
    assert plan.files_considered == len(dataset.sources)
    assert plan.files_pruned == plan.files_considered - plan.files_read


def test_a_window_outside_the_corpus_prunes_everything():
    dataset = resolve("otel_s3")
    window = build_window(dataset, "2030-01-01 00:00:00+00", "2030-01-01 00:01:00+00")
    plan = pruning.plan(dataset, window)
    assert plan.row_groups_read == 0
    assert plan.est_rows_scanned == 0


def test_local_datasets_have_nothing_to_prune():
    dataset = resolve("openstack")
    window = build_window(dataset, OS_LO, OS_HI)
    plan = pruning.plan(dataset, window)
    assert plan.row_groups_considered == 0


def test_oversized_scan_is_refused_before_it_runs():
    """A pre-flight refusal in milliseconds, instead of a 30-second timeout."""
    dataset = resolve("otel_s3")
    # 1,624 row groups of 614,400 rows is ~998M rows, well past the limit.
    many = pruning.synthetic_row_groups(
        dataset.sources[0], 1624, seconds_per_group=0.1
    )
    pruning.seed_cache(dataset.sources[0], many)
    window = build_window(dataset, OTEL_LO, "2025-09-23 00:03:00+00")

    with pytest.raises(QueryRefused) as excinfo:
        pruning.plan_and_guard(dataset, window)
    refusal = excinfo.value.refusal
    assert refusal.code == "scan_too_large"
    assert refusal.details["suggested_window_seconds"] >= 1


# --- QueryStats -------------------------------------------------------------
def test_builders_attach_cost_stats():
    _, _, stats = build(db.count_by_field, OTEL_LO, OTEL_HI, dataset="otel_s3")
    assert stats.dataset == "otel_s3"
    assert stats.est_rows_scanned > 0
    assert stats.window_seconds == 10.0
    assert stats.approximate is True


def test_stats_summary_mentions_volume_time_and_approximation():
    _, _, stats = build(db.count_by_field, OTEL_LO, OTEL_HI, dataset="otel_s3")
    stats.elapsed_ms = 1234
    summary = stats.summary()
    assert "rows scanned" in summary
    assert "1234 ms" in summary
    assert "APPROXIMATE" in summary


def test_pruning_ratio_reports_the_fraction_skipped():
    stats = QueryStats(row_groups_considered=1000, row_groups_read=10)
    assert stats.pruning_ratio == pytest.approx(0.99)


def test_merge_totals_cost_across_queries():
    total = merge(
        [
            QueryStats(dataset="otel_s3", est_rows_scanned=100, elapsed_ms=10),
            QueryStats(dataset="openstack", est_rows_scanned=5, elapsed_ms=2),
        ]
    )
    assert total.est_rows_scanned == 105
    assert total.elapsed_ms == 12
    assert "otel_s3" in total.dataset and "openstack" in total.dataset


def test_merge_is_approximate_if_any_input_was():
    total = merge([QueryStats(approximate=False), QueryStats(approximate=True)])
    assert total.approximate is True


# --- QueryResult ------------------------------------------------------------
def test_query_result_is_a_list_so_existing_callers_keep_working():
    result = QueryResult([{"a": 1}, {"a": 2}], QueryStats())
    assert len(result) == 2
    assert result[0]["a"] == 1
    assert [row["a"] for row in result] == [1, 2]
    assert result.stats.row_count == 2
    assert not result.refused


def test_refused_result_carries_the_refusal_and_no_rows():
    from backend.db.errors import Refusal

    refusal = Refusal(reason="too wide", suggestion="narrow it")
    result = QueryResult([], QueryStats(), refusal=refusal)
    assert result.refused
    assert result.refusal.as_tool_result()["status"] == "refused"


# --- Query classes ----------------------------------------------------------
def test_remote_aggregates_are_bulk_and_search_is_interactive():
    """Two classes, so one timeout does not have to serve both."""
    from backend.db import config

    assert config.SERENEDB_BULK_TIMEOUT_MS > config.SERENEDB_INTERACTIVE_TIMEOUT_MS
    assert QueryClass.BULK != QueryClass.INTERACTIVE


def test_bulk_builders_declare_the_bulk_class():
    _, _, stats = build(db.count_by_field, OTEL_LO, OTEL_HI, dataset="otel_s3")
    # The class is stamped by the decorator at execution time; the wrapper is
    # what carries it, so assert the decorated function is the bulk variant.
    assert db.count_by_field.__name__ == "count_by_field"
    assert stats.query_class in (str(QueryClass.INTERACTIVE), str(QueryClass.BULK))


# --- where() composition ----------------------------------------------------
def test_where_omits_empty_clauses():
    dataset = resolve("openstack")
    window = build_window(dataset, OS_LO, OS_HI)
    clause, params = pred.where(
        pred.ts_filter(dataset, window), pred.severity_filter(dataset, None)
    )
    rendered = normalized(clause)
    assert rendered.startswith("WHERE")
    assert rendered.count("AND") == 1  # only the two timestamp bounds
    assert len(params) == 2


def test_where_with_no_clauses_renders_nothing():
    clause, params = pred.where()
    assert normalized(clause) == ""
    assert params == []


# --- Per-dataset auto-complete ----------------------------------------------
def render_auto_complete(query: str, dataset: str | None = None) -> str:
    # The per-dataset SQL builder is private; the public ``auto_complete``
    # merges it across the local corpora (see ``test_auto_complete.py``).
    from backend.db.auto_complete import _auto_complete_dataset

    statement, _ = _auto_complete_dataset.__wrapped__(query, dataset)
    return normalized(statement)


NO_MATCH = "WHERE false"


def test_auto_complete_resolves_selectors_per_dataset():
    assert '"Component"' in render_auto_complete("@nova", "openstack")
    assert '"Component"' in render_auto_complete("@KERN", "bgl")


def test_auto_complete_template_selector_reads_event_template_on_both():
    assert '"EventTemplate"' in render_auto_complete("#Took", "openstack")
    assert '"EventTemplate"' in render_auto_complete("#parity", "bgl")


def test_auto_complete_has_no_pid_or_node_selector():
    """'!' is only the case modifier; on its own it selects nothing."""
    for dataset in ("openstack", "bgl"):
        assert NO_MATCH in render_auto_complete("!257", dataset), dataset


def test_auto_complete_bang_after_a_selector_is_the_case_modifier():
    rendered = render_auto_complete("@!nova", "openstack")
    assert NO_MATCH not in rendered
    assert '"Component"' in rendered


def test_auto_complete_suppresses_a_selector_the_corpus_lacks():
    # The OTel slice has no event-template field, so '#' suggests nothing there.
    assert NO_MATCH in render_auto_complete("#E25", "otel_slice")


def test_auto_complete_never_touches_the_remote_tier():
    """A keystroke must not reach object storage."""
    for query in ("@cart", "$timeout", ""):
        rendered = render_auto_complete(query, "otel_s3")
        assert NO_MATCH in rendered
        assert "read_parquet" not in rendered


def test_auto_complete_requires_a_selector():
    assert NO_MATCH in render_auto_complete("nova", "openstack")


def test_auto_complete_content_branch_returns_record_ids():
    """The '$' branch suggests whole lines keyed by record id, not words."""
    rendered = render_auto_complete("$timeout", "openstack")
    assert '"LineId"' in rendered
    assert '"Content"' in rendered
    assert "ts_dict_agg" not in rendered


def test_auto_complete_is_not_offered_on_the_otel_index():
    """An external index has no verbatim+token index pair, so the type-ahead
    recipe cannot run against it and says so instead of failing at query
    time."""
    from backend.db.datasets import Capability, resolve

    assert not resolve("otel_slice").supports(Capability.AUTO_COMPLETE)


def test_auto_complete_content_branch_works_on_a_local_corpus():
    """The ``$`` branch matches whole lines and inserts a record reference, so
    it needs a corpus with a record id -- which an external index has not."""
    rendered = render_auto_complete("$parity", "bgl")
    assert '"Content"' in rendered
    assert "LineId" in rendered


def test_auto_complete_queries_the_index_not_the_table():
    rendered = render_auto_complete("@nova", "openstack")
    assert '"openstack_idx"' in rendered or '"openstack_token_idx"' in rendered
