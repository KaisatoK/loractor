"""The dataset registry.

Offline: registrations are data, and SQL shapes are rendered by
``tests/sql_render.py`` rather than by a database.

The load-bearing test here is
:func:`test_openstack_registration_matches_the_pre_registry_constants`: the
registry must reproduce the original hardcoded behaviour exactly, because the
OpenStack demo has to keep answering identically.
"""

from __future__ import annotations

import pytest

from backend.db import datasets as ds
from backend.db.config import TABLE_NAME
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


# --- The compatibility contract ---------------------------------------------
def test_openstack_registration_matches_the_pre_registry_constants():
    """Same table, same indexes, same columns as before the registry existed."""
    d = ds.resolve("openstack")

    assert d.sources == (TABLE_NAME,)
    assert d.index_name == f"{TABLE_NAME}_idx"
    assert d.token_index_name == f"{TABLE_NAME}_token_idx"

    # Column names the original queries used verbatim.
    assert d.column_for(ds.TS) == "log_timestamp"
    assert d.column_for(ds.SEVERITY) == "category"
    assert d.column_for(ds.SERVICE) == "service"
    assert d.column_for(ds.MESSAGE) == "content"
    assert d.column_for(ds.ENTITY_ID) == "pid"
    assert d.column_for(ds.TEMPLATE) == "event_template"
    assert d.column_for(ds.EVENT_ID) == "event_id"
    assert d.column_for(ds.RECORD_ID) == "line_id"


def test_openstack_keeps_its_original_severity_vocabulary():
    # Upper case here, unlike the OTel corpus. Both are measured, not assumed.
    assert ds.resolve("openstack").severity_values == ("INFO", "WARNING", "ERROR")


def test_openstack_keeps_its_original_selector_map():
    d = ds.resolve("openstack")
    assert d.selector_field("@") == ds.SERVICE
    assert d.selector_field("#") == ds.EVENT_ID
    assert d.selector_field("^") == ds.TEMPLATE
    assert d.selector_field("!") == ds.ENTITY_ID
    assert d.selector_field("$") == ds.MESSAGE


def test_unknown_selector_resolves_to_none():
    assert ds.resolve("openstack").selector_field("%") is None


# --- Relations ---------------------------------------------------------------
def test_native_table_scans_the_table_itself():
    assert normalized(ds.resolve("openstack").scan_relation()) == f'"{TABLE_NAME}"'


def test_native_table_searches_the_index_relation():
    # Search goes through the index, never the base table.
    assert normalized(ds.resolve("openstack").search_relation()) == f'"{TABLE_NAME}_idx"'


# --- Fields ------------------------------------------------------------------
def test_sql_for_quotes_a_column_as_an_identifier():
    assert normalized(ds.resolve("openstack").sql_for(ds.MESSAGE)) == '"content"'


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
    assert "content" not in message  # lists canonical names, not columns
    assert ds.MESSAGE in message


# --- Capabilities ------------------------------------------------------------
def test_openstack_declares_the_capabilities_it_actually_has():
    d = ds.resolve("openstack")
    for capability in (
        ds.Capability.BM25,
        ds.Capability.VECTOR,
        ds.Capability.RECORD_ID,
        ds.Capability.EXACT_COUNTS,
        ds.Capability.TEMPLATES,
        ds.Capability.AUTO_COMPLETE,
    ):
        assert d.supports(capability), capability
    # The 2k-row demo corpus is not a sweep target.
    assert not d.supports(ds.Capability.CHUNK_SWEEP)


def test_exact_counts_shortcut_matches_the_capability():
    d = ds.resolve("openstack")
    assert d.exact_counts is d.supports(ds.Capability.EXACT_COUNTS)


def test_datasets_with_filters_by_capability():
    assert ds.OPENSTACK in ds.datasets_with(ds.Capability.VECTOR)


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
    # (A naive substring check on TABLE_NAME would false-positive on the word
    # "log" in the prose description.)
    for entry in catalog.values():
        assert "sources" not in entry
        assert "index_name" not in entry
        assert "token_index_name" not in entry

    blob = repr(catalog)
    for identifier in (
        "log_timestamp",
        "log_idx",
        "log_token_idx",
        "event_template",
        "pid_text",
        "line_id",
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
