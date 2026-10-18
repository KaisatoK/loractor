"""The dataset registry.

Offline: registrations are data, and SQL shapes are rendered by
``tests/sql_render.py`` rather than by a database.

The load-bearing test here is
:func:`test_openstack_registration_matches_the_loghub_table`: the registry must
describe the single ``openstack`` table exactly as the loghub loader builds it,
because every query is composed from these declarations.
"""

from __future__ import annotations

from typing import NamedTuple

import pytest

import backend.db as db
from backend.db import datasets as ds
from backend.db import loghub
from backend.db.errors import QueryRefused
from sql_render import normalized


# --- Resolution --------------------------------------------------------------
def test_resolve_returns_the_registration():
    assert ds.resolve("openstack").id == "openstack"


def test_resolve_none_gives_the_default_dataset():
    assert ds.resolve(None).id == ds.DEFAULT_DATASET_ID


def test_resolve_unknown_id_raises_and_names_the_known_ids():
    with pytest.raises(ds.UnknownDatasetError) as excinfo:
        ds.resolve("nope")
    assert "openstack" in str(excinfo.value)


def test_dataset_ids_are_sorted_and_non_empty():
    ids = ds.dataset_ids()
    assert ids == tuple(sorted(ids))
    assert "openstack" in ids


def test_register_rejects_a_duplicate_id():
    with pytest.raises(ValueError):
        ds.register(ds.OPENSTACK)


# --- The OpenStack registration ---------------------------------------------
def test_openstack_registration_matches_the_loghub_table():
    """One table, loaded by loghub from three files, with loghub column names."""
    d = ds.resolve("openstack")

    assert d.kind is ds.DatasetKind.NATIVE_TABLE
    assert d.sources == ("openstack",)
    assert d.index_name == "openstack_idx"
    assert d.token_index_name == "openstack_token_idx"
    assert d.ts_cast == "timestamptz"
    assert d.row_estimate == 207_820

    # The same Level/Component naming BGL uses.
    assert d.column_for(ds.TS) == "Timestamp"
    assert d.column_for(ds.SEVERITY) == "Level"
    assert d.column_for(ds.SERVICE) == "Component"
    assert d.column_for(ds.MESSAGE) == "Content"
    assert d.column_for(ds.ENTITY_ID) == "Pid"
    assert d.column_for(ds.TEMPLATE) == "EventTemplate"
    assert d.column_for(ds.RECORD_ID) == "LineId"
    # The raw logs carry no event id, so none is mapped.
    assert not d.has_field(ds.EVENT_ID)


def test_openstack_pid_has_no_text_mirror():
    # No PidText column: the integer pid is neither indexed nor suggestable.
    entity = ds.resolve("openstack").field(ds.ENTITY_ID)
    assert entity.autocomplete_column is None
    assert not entity.text_indexed and not entity.token_indexed


def test_openstack_severity_vocabulary_includes_critical():
    assert ds.resolve("openstack").severity_values == (
        "INFO",
        "WARNING",
        "ERROR",
        "CRITICAL",
    )


def test_openstack_selector_map():
    d = ds.resolve("openstack")
    assert d.selector_field("@") == ds.SERVICE
    assert d.selector_field("#") == ds.TEMPLATE
    assert d.selector_field("$") == ds.MESSAGE
    # No event-id selector, and '!' is only the case modifier, never a selector.
    assert d.selector_field("?") is None
    assert d.selector_field("!") is None


def test_no_dataset_offers_the_bang_selector():
    for dataset in ds.all_datasets():
        assert "!" not in dataset.selectors, dataset.id


def test_default_dataset_is_openstack():
    assert ds.DEFAULT_DATASET_ID == "openstack"


def test_unknown_selector_resolves_to_none():
    assert ds.resolve("openstack").selector_field("%") is None


# --- Relations ---------------------------------------------------------------
def test_native_table_scans_the_table_itself():
    assert normalized(ds.resolve("openstack").scan_relation()) == '"openstack"'


def test_native_table_searches_the_index_relation():
    # Search goes through the index, never the base table.
    assert normalized(ds.resolve("openstack").search_relation()) == '"openstack_idx"'


# --- Fields ------------------------------------------------------------------
def test_sql_for_quotes_a_column_as_an_identifier():
    assert normalized(ds.resolve("openstack").sql_for(ds.MESSAGE)) == '"Content"'


def test_expression_fields_render_verbatim():
    f = ds.Field(expression="lower(\"Body\")")
    assert normalized(f.as_sql()) == 'lower("Body")'


def test_field_requires_a_column_or_an_expression():
    with pytest.raises(ValueError):
        ds.Field()


def test_missing_field_raises_and_lists_what_is_available():
    d = ds.resolve("openstack")
    with pytest.raises(ds.UnsupportedFieldError) as excinfo:
        d.field("not_a_field")
    message = str(excinfo.value)
    assert "openstack" in message
    assert "Content" not in message  # lists canonical names, not columns
    assert ds.MESSAGE in message


# --- Capabilities ------------------------------------------------------------
def test_openstack_declares_the_capabilities_it_actually_has():
    d = ds.resolve("openstack")
    for capability in (
        ds.Capability.BM25,
        ds.Capability.RECORD_ID,
        ds.Capability.EXACT_COUNTS,
        ds.Capability.TEMPLATES,
        ds.Capability.AUTO_COMPLETE,
    ):
        assert d.supports(capability), capability
    # Loaded with --no-embed: no per-row vectors.
    assert not d.supports(ds.Capability.VECTOR)
    # A local corpus is not a sweep target.
    assert not d.supports(ds.Capability.CHUNK_SWEEP)


def test_exact_counts_shortcut_matches_the_capability():
    d = ds.resolve("openstack")
    assert d.exact_counts is d.supports(ds.Capability.EXACT_COUNTS)


def test_datasets_with_filters_by_capability():
    assert ds.OPENSTACK in ds.datasets_with(ds.Capability.AUTO_COMPLETE)
    assert ds.OPENSTACK not in ds.datasets_with(ds.Capability.VECTOR)


# --- Model-facing catalog ----------------------------------------------------
def test_catalog_describes_every_dataset_without_leaking_identifiers():
    catalog = ds.catalog()
    assert set(catalog) == set(ds.dataset_ids())

    entry = catalog["openstack"]
    assert entry["description"]
    assert entry["exact_counts"] is True
    assert ds.MESSAGE in entry["fields"]
    assert "INFO" in entry["severity_values"]

    # No table, index, or column name may reach the model through the catalog.
    # Checked structurally: the keys that hold identifiers must be absent, and
    # the unambiguous identifier strings must not appear anywhere in the values.
    # (A naive substring check on a table name would false-positive on the
    # dataset id, which the catalog is keyed by.)
    for entry in catalog.values():
        assert "sources" not in entry
        assert "index_name" not in entry
        assert "token_index_name" not in entry

    blob = repr(catalog)
    for identifier in (
        "openstack_idx",
        "openstack_token_idx",
        "EventTemplate",
        "LineId",
    ):
        assert identifier not in blob, identifier


def test_every_registration_is_internally_consistent():
    for dataset in ds.all_datasets():
        assert dataset.id
        assert dataset.sources
        assert dataset.severity_values
        assert dataset.max_rows > 0
        # Every selector must point at a field the dataset really has.
        for char, name in dataset.selectors.items():
            assert dataset.has_field(name), (dataset.id, char, name)
        # Indexes are required by the capabilities that depend on them.
        if dataset.supports(ds.Capability.BM25):
            assert dataset.index_name
        if dataset.supports(ds.Capability.AUTO_COMPLETE):
            assert dataset.index_name and dataset.token_index_name
        # A canonical field name outside the vocabulary is a typo.
        for name in dataset.fields:
            assert name in ds.CANONICAL_FIELDS, (dataset.id, name)


# --- The corpus contract, parametrized over the local corpora ----------------
# Every local corpus must honour the same registry contract, and adding one
# required no new SQL in any builder. These cases were duplicated one-per-corpus
# in ``test_openstack.py`` / ``test_bgl.py``; the per-corpus facts live in the
# table and the assertions are written once.
class _CorpusContract(NamedTuple):
    dataset_id: str
    window: tuple[str, str]
    capabilities: tuple
    absent_capabilities: tuple
    severity_values: tuple[str, ...]
    # (canonical name, real column, a distinctive fragment of the SQL
    # expression). Exactly one of the last two is set: a plain column, or a
    # code-owned expression such as OpenStack's derived request id.
    field_mappings: tuple[tuple[str, str | None, str | None], ...]
    absent_selectors: tuple[str, ...]
    index_name: str
    token_index_name: str
    ts_cast: str
    search_text: str
    trend_field: str
    trend_bucket: str
    severity_only_must_contain: tuple[str, ...]
    severity_only_must_not_contain: tuple[str, ...]


CORPORA = (
    _CorpusContract(
        dataset_id="openstack",
        window=("2017-05-14 00:00:00+00", "2017-05-18 00:00:00+00"),
        capabilities=(
            ds.Capability.BM25,
            ds.Capability.RECORD_ID,
            ds.Capability.EXACT_COUNTS,
            ds.Capability.TEMPLATES,
            ds.Capability.AUTO_COMPLETE,
        ),
        absent_capabilities=(ds.Capability.VECTOR, ds.Capability.CHUNK_SWEEP),
        severity_values=("INFO", "WARNING", "ERROR", "CRITICAL"),
        field_mappings=(
            ("ts", "Timestamp", None),
            ("severity", "Level", None),
            ("service", "Component", None),
            ("message", "Content", None),
            ("entity_id", "Pid", None),
            ("template", "EventTemplate", None),
            ("record_id", "LineId", None),
            # Derived, not a column: the request id is cut out of ``ADDR``.
            ("request_id", None, '"ADDR"'),
        ),
        absent_selectors=("?", "!"),
        index_name="openstack_idx",
        token_index_name="openstack_token_idx",
        ts_cast="timestamptz",
        search_text="instance spawned",
        trend_field="nova.compute.manager",
        trend_bucket="hour",
        severity_only_must_contain=('"Level"', '"Timestamp"', '"openstack"'),
        severity_only_must_not_contain=('"Node"', '"SeverityText"', '"ServiceName"'),
    ),
    _CorpusContract(
        dataset_id="bgl",
        window=("2005-06-01 00:00:00+00", "2006-02-01 00:00:00+00"),
        capabilities=(
            ds.Capability.BM25,
            ds.Capability.RECORD_ID,
            ds.Capability.EXACT_COUNTS,
            ds.Capability.TEMPLATES,
            ds.Capability.AUTO_COMPLETE,
        ),
        absent_capabilities=(ds.Capability.VECTOR, ds.Capability.CHUNK_SWEEP),
        severity_values=("INFO", "WARNING", "ERROR", "SEVERE", "FATAL", "FAILURE"),
        field_mappings=(
            ("ts", "Timestamp", None),
            ("severity", "Level", None),
            ("service", "Component", None),
            ("message", "Content", None),
            ("entity_id", "Node", None),
            ("template", "EventTemplate", None),
            ("record_id", "LineId", None),
        ),
        absent_selectors=("?",),
        index_name="bgl_idx",
        token_index_name="bgl_token_idx",
        ts_cast="timestamptz",
        search_text="parity error",
        trend_field="KERNEL",
        trend_bucket="month",
        severity_only_must_contain=('"Level"', '"bgl"'),
        severity_only_must_not_contain=("category", "log_timestamp"),
    ),
)

each_corpus = pytest.mark.parametrize(
    "corpus", CORPORA, ids=[c.dataset_id for c in CORPORA]
)


def _build(fn, *args, **kwargs):
    return fn.__wrapped__(*args, **kwargs)


@each_corpus
def test_each_corpus_declares_what_it_can_do(corpus):
    dataset = ds.resolve(corpus.dataset_id)
    for capability in corpus.capabilities:
        assert dataset.supports(capability), capability


@each_corpus
def test_each_corpus_does_not_support_what_it_lacks(corpus):
    dataset = ds.resolve(corpus.dataset_id)
    for capability in corpus.absent_capabilities:
        assert not dataset.supports(capability), capability


@each_corpus
def test_each_corpus_maps_the_canonical_fields_it_actually_has(corpus):
    dataset = ds.resolve(corpus.dataset_id)
    for name, column, expression in corpus.field_mappings:
        field = dataset.field(name)
        if column is not None:
            assert field.column == column, (corpus.dataset_id, name)
            assert field.expression is None, (corpus.dataset_id, name)
            # ...and the SQL the builders compose names that same column.
            assert f'"{column}"' in normalized(dataset.sql_for(name)), (
                corpus.dataset_id,
                name,
            )
        else:
            assert field.column is None, (corpus.dataset_id, name)
            assert field.expression and expression in field.expression, (
                corpus.dataset_id,
                name,
            )
            assert expression in normalized(dataset.sql_for(name)), (
                corpus.dataset_id,
                name,
            )


@each_corpus
def test_each_corpus_does_not_claim_an_event_id_it_does_not_have(corpus):
    dataset = ds.resolve(corpus.dataset_id)
    assert "event_id" not in dataset.fields
    for selector in corpus.absent_selectors:
        assert selector not in dataset.selectors
    with pytest.raises((QueryRefused, KeyError)):
        dataset.sql_for("event_id")


@each_corpus
def test_each_corpus_owns_its_severity_vocabulary(corpus):
    severities = ds.resolve(corpus.dataset_id).severity_values
    assert severities == corpus.severity_values
    assert all(s.isupper() for s in severities)


@each_corpus
def test_every_builder_composes_against_each_corpus(corpus):
    cases = (
        (db.count_by_field, corpus.window, {"dataset": corpus.dataset_id}),
        (
            db.count_distinct_by_field,
            corpus.window,
            {"dataset": corpus.dataset_id, "field": "service"},
        ),
        (
            db.top_by_field,
            corpus.window,
            {"dataset": corpus.dataset_id, "field": "service"},
        ),
        (
            db.get_field_values,
            (*corpus.window, "template"),
            {"dataset": corpus.dataset_id},
        ),
        (
            db.get_trend,
            (*corpus.window, "service", corpus.trend_field),
            {"dataset": corpus.dataset_id, "bucket": corpus.trend_bucket},
        ),
        (
            db.search_logs,
            (corpus.search_text,),
            {"dataset": corpus.dataset_id},
        ),
        (db.get_log_by_record_id, (1,), {"dataset": corpus.dataset_id}),
    )
    for fn, args, kwargs in cases:
        rendered = normalized(_build(fn, *args, **kwargs)[0])
        assert (
            f'"{corpus.dataset_id}"' in rendered
            or f"{corpus.dataset_id}_" in rendered
        ), fn.__name__


@each_corpus
def test_builders_reference_each_corpus_columns_not_other_corpora(corpus):
    severity_only = normalized(
        _build(db.count_by_field, *corpus.window, dataset=corpus.dataset_id)[0]
    )
    for token in corpus.severity_only_must_contain:
        assert token in severity_only
    for foreign in corpus.severity_only_must_not_contain:
        assert foreign not in severity_only

    by_service = normalized(
        _build(
            db.top_by_field,
            *corpus.window,
            dataset=corpus.dataset_id,
            field="service",
        )[0]
    )
    assert '"Component"' in by_service
    assert '"ServiceName"' not in by_service


@each_corpus
def test_search_uses_each_corpus_index_relation(corpus):
    rendered = normalized(
        _build(db.search_logs, corpus.search_text, dataset=corpus.dataset_id)[0]
    )
    assert corpus.index_name in rendered
    assert "read_parquet" not in rendered


@each_corpus
def test_each_corpus_timestamps_are_cast_as_timestamptz(corpus):
    assert ds.resolve(corpus.dataset_id).ts_cast == corpus.ts_cast


@each_corpus
def test_the_loader_spec_is_registered_for_each_corpus(corpus):
    spec = loghub.spec_for(corpus.dataset_id)
    assert spec.dataset_id == corpus.dataset_id
    assert spec.csv_name.endswith("_structured.csv")


@each_corpus
def test_the_loader_names_indexes_from_each_corpus_registry(corpus):
    dataset = ds.resolve(corpus.dataset_id)
    assert dataset.index_name == corpus.index_name
    assert dataset.token_index_name == corpus.token_index_name
