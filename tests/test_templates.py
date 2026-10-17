"""Template-level embeddings and the semantic search branch.

Offline: naming, capability gating, SQL shape, and search-mode selection. The
embedding build itself needs a live server and an embedding service.

The premise these tests protect: embedding every row does not scale, embedding
every *template* does. Measured on the built slice, 150,557 rows reduce to 768
distinct templates -- a 196x reduction, and the difference between ~64 seconds of
embedding and ~3.5 hours.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from sql_render import render  # noqa: E402

from backend.db import templates  # noqa: E402
from backend.db.datasets import Capability, resolve  # noqa: E402
from backend.db.errors import QueryRefused  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_vector_cache():
    """Template-vector availability is process-cached, so each test starts clean.

    Without this a test that records availability would change the SQL composed
    by every later test.
    """
    templates.clear_vector_cache()
    yield
    templates.clear_vector_cache()


# --- Naming -----------------------------------------------------------------
def test_each_dataset_gets_its_own_template_table():
    assert templates.template_table("otel_slice") == "otel_slice_templates"
    assert templates.template_table("openstack") == "openstack_templates"


def test_a_dataset_object_is_accepted_as_well_as_an_id():
    assert templates.template_table(resolve("otel_slice")) == "otel_slice_templates"


# --- Capability gating ------------------------------------------------------
def test_a_dataset_without_templates_is_refused():
    """Every registered corpus happens to parse templates, so this uses a
    synthetic one -- the guard must hold for a future corpus that does not."""
    from types import MappingProxyType

    from backend.db.datasets import Dataset, DatasetKind, Field

    plain = Dataset(
        id="plain",
        kind=DatasetKind.NATIVE_TABLE,
        description="A corpus with no parsed templates.",
        sources=("plain_log",),
        fields=MappingProxyType({"message": Field(column="body")}),
        capabilities=frozenset({Capability.BM25}),
        severity_values=(),
        ts_cast="timestamptz",
    )

    with pytest.raises(QueryRefused) as excinfo:
        templates._require_templates(plain)
    assert excinfo.value.refusal.code == "unsupported_capability"


def test_every_registered_corpus_currently_parses_templates():
    """Documents why the test above needs a synthetic dataset."""
    from backend.db.datasets import all_datasets

    assert all(d.supports(Capability.TEMPLATES) for d in all_datasets())


def test_the_slice_declares_templates():
    assert resolve("otel_slice").supports(Capability.TEMPLATES)


def test_missing_template_table_never_breaks_search(monkeypatch):
    """Absence must degrade to lexical search, not raise.

    The table is built by an operator action, so it is legitimately absent on a
    fresh database and search still has to work.
    """
    def explode(_dataset):
        raise RuntimeError("no such table")

    monkeypatch.setattr(templates, "templates_status", explode)
    assert templates.has_template_vectors("otel_slice") is False


def test_an_empty_template_table_is_not_usable(monkeypatch):
    monkeypatch.setattr(
        templates,
        "templates_status",
        lambda _d: {"exists": True, "embedded": 0, "templates": 0},
    )
    assert templates.has_template_vectors("otel_slice") is False


def test_a_populated_template_table_is_usable(monkeypatch):
    monkeypatch.setattr(
        templates,
        "templates_status",
        lambda _d: {"exists": True, "embedded": 768, "templates": 768},
    )
    assert templates.has_template_vectors("otel_slice") is True


# --- Branch SQL -------------------------------------------------------------
def test_the_branch_produces_the_fusion_contract():
    """Must yield (rid, rank) so it composes with the existing RRF."""
    branch, params = templates.template_branch(resolve("otel_slice"))
    text = render(branch)
    assert "rid" in text
    assert "rank" in text
    assert params == []


def test_the_branch_reads_the_template_table_and_joins_to_rows():
    text = render(templates.template_branch(resolve("otel_slice"))[0])
    assert "otel_slice_templates" in text
    assert "query_embedding" in text


def test_the_branch_bounds_both_distance_and_width():
    """Unbounded, a vague query would expand to most of the corpus."""
    text = render(templates.template_branch(resolve("otel_slice"))[0])
    assert str(templates.MAX_TEMPLATE_DISTANCE) in text
    assert str(templates.TEMPLATE_BRANCH_WIDTH) in text


def test_the_branch_joins_on_template_text_not_a_row_column():
    """Rows are reached by template equality, so rebuilding templates never
    requires rewriting the base table."""
    text = render(templates.template_branch(resolve("otel_slice"))[0])
    assert "template_text" in text


# --- Search mode selection --------------------------------------------------
def test_the_slice_uses_the_template_branch_when_vectors_exist(monkeypatch):
    from backend.db import query

    monkeypatch.setattr(templates, "has_template_vectors", lambda _d: True)
    built = query.search_logs.__wrapped__(
        "money conversion problem", dataset="otel_slice", limit=3
    )
    text = render(built[0])
    assert "otel_slice_templates" in text
    assert "query_embedding" in text


def test_the_slice_falls_back_to_lexical_without_vectors(monkeypatch):
    from backend.db import query

    monkeypatch.setattr(templates, "has_template_vectors", lambda _d: False)
    text = render(
        query.search_logs.__wrapped__("timeout", dataset="otel_slice", limit=3)[0]
    )
    assert "otel_slice_templates" not in text
    assert "BM25" in text


def test_openstack_keeps_its_per_row_embeddings(monkeypatch):
    """The per-row path must not be replaced by the template path.

    OpenStack is 2,000 rows, where per-row embedding is cheap and strictly more
    precise. A regression here would silently coarsen its search.
    """
    from backend.db import query

    monkeypatch.setattr(templates, "has_template_vectors", lambda _d: True)
    text = render(
        query.search_logs.__wrapped__("spawn failure", dataset="openstack", limit=3)[0]
    )
    assert "openstack_templates" not in text
    assert "embedding <=>" in text


def test_the_template_mode_discloses_itself_in_the_stats(monkeypatch):
    """A template match describes a kind of event, not an individual line, and
    an answer built on it must be able to say so."""
    from backend.db import query

    monkeypatch.setattr(templates, "has_template_vectors", lambda _d: True)
    built = query.search_logs.__wrapped__(
        "money conversion problem", dataset="otel_slice", limit=3
    )
    stats = built[2]
    assert any("template-level" in note for note in stats.notes)


def test_rrf_weighting_is_unchanged():
    """The fusion constant is shared with the per-row path and is not retuned."""
    from backend.db.query import RRF_K

    assert RRF_K == 60


def test_embedding_batches_are_bounded():
    assert 0 < templates.EMBED_BATCH <= 1000
