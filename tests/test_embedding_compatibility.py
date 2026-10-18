"""Compatibility of the configured embedding model with the stored vectors.

Offline. Changing the embedding model breaks template embeddings in three ways,
each silent or obscure on its own:

* the database cannot embed at all (the server-side secret is missing, which is
  the state after the container is recreated);
* the model's native width differs from the column's, and the ``::FLOAT[n]``
  cast truncates rather than rejects;
* the stored vectors came from another model, so every distance against a new
  query vector is meaningless while looking perfectly healthy.

These assert each one is detected and reported with the fix.
"""

from __future__ import annotations

import contextlib

import pytest

from backend.db import templates
from backend.db.errors import QueryRefused


def _cursor_with(monkeypatch, *, dimension=None, embed_error=None, stored=None):
    """Install an ``_embed_cursor`` whose embedding and build record are fixed."""

    class Cur:
        def execute(self, statement, params=None):
            text = str(statement)
            if "ai_embed" in text:
                if embed_error:
                    raise RuntimeError(embed_error)
                self._row = {"emb": [0.0] * dimension}
            elif "information_schema" in text:
                self._row = {"n": 1}
            elif "FROM" in text and "model" in text:
                self._row = stored
            else:
                self._row = {"n": 0, "templates": 1, "embedded": 1, "rows_covered": 1}

        def fetchone(self):
            return getattr(self, "_row", None)

        def fetchall(self):
            return []

    @contextlib.contextmanager
    def cursor():
        yield Cur()

    monkeypatch.setattr(templates, "_embed_cursor", cursor)


# --- The three failure modes ------------------------------------------------
def test_a_missing_secret_is_reported_with_the_fix(monkeypatch):
    """ai_embed resolves its endpoint from a server-side secret, so the model
    name in Python says nothing about what the database can do."""
    _cursor_with(monkeypatch, embed_error="ai_embed: secret 'local_ai' not found")
    report = templates.embedding_compatibility("bgl")
    assert report["ok"] is False and report["can_embed"] is False
    assert any("cannot embed" in p for p in report["problems"])
    assert any("secret" in p and "container" in p for p in report["problems"])


def test_a_width_mismatch_is_detected_and_names_the_value_to_set(monkeypatch):
    """gemini-embedding-2 returns 3072 natively while the table stores 768; the
    cast would truncate instead of refusing."""
    monkeypatch.setattr(templates, "EMBEDDING_DIMENSION", 768)
    _cursor_with(monkeypatch, dimension=3072)
    report = templates.embedding_compatibility("bgl")
    assert report["can_embed"] is True
    assert report["actual_dimension"] == 3072
    assert report["ok"] is False
    assert any("EMBEDDING_DIMENSION=3072" in p for p in report["problems"])


def test_vectors_from_another_model_are_reported_as_meaningless(monkeypatch):
    monkeypatch.setattr(templates, "EMBEDDING_MODEL", "gemini-embedding-2")
    monkeypatch.setattr(templates, "EMBEDDING_DIMENSION", 768)
    _cursor_with(monkeypatch, dimension=768,
                 stored={"model": "nomic-embed-text:v1.5", "dimension": 768,
                         "built_at": "2026-01-01 00:00:00+00"})
    report = templates.embedding_compatibility("bgl")
    assert report["ok"] is False
    assert any("meaningless" in p and "rebuild" in p for p in report["problems"])


def test_a_matching_model_and_width_is_compatible(monkeypatch):
    monkeypatch.setattr(templates, "EMBEDDING_MODEL", "gemini-embedding-2")
    monkeypatch.setattr(templates, "EMBEDDING_DIMENSION", 768)
    _cursor_with(monkeypatch, dimension=768,
                 stored={"model": "gemini-embedding-2", "dimension": 768,
                         "built_at": "2026-01-01 00:00:00+00"})
    report = templates.embedding_compatibility("bgl")
    assert report["ok"] is True and report["problems"] == []


# --- The build refuses before spending anything ----------------------------
def test_a_build_is_refused_before_it_embeds_anything(monkeypatch):
    """A missing secret used to surface on the first batch, after the table and
    the template extraction were already done."""
    monkeypatch.setattr(
        templates, "embedding_compatibility",
        lambda ds: {"model": "gemini-embedding-2", "declared_dimension": 768,
                    "actual_dimension": None, "stored": None, "can_embed": False,
                    "ok": False, "problems": ["the database cannot embed"]},
    )
    monkeypatch.setattr(
        templates, "_embed_cursor",
        lambda: pytest.fail("the build must not open a cursor when incompatible"),
    )
    with pytest.raises(QueryRefused) as caught:
        templates.build_templates("bgl")
    assert caught.value.refusal.code == "embedding_incompatible"


def test_a_rebuild_may_replace_vectors_from_another_model(monkeypatch):
    """Mixing models in one table is the danger; a rebuild drops the table
    first, so a differing stored model must not block it."""
    opened: list[str] = []
    monkeypatch.setattr(
        templates, "embedding_compatibility",
        lambda ds: {"model": "gemini-embedding-2", "declared_dimension": 768,
                    "actual_dimension": 768, "can_embed": True,
                    "stored": {"model": "nomic-embed-text:v1.5"}, "ok": False,
                    "problems": ["the stored vectors were built with 'nomic-embed-text:v1.5' ..."]},
    )

    @contextlib.contextmanager
    def cursor():
        opened.append("yes")
        raise RuntimeError("stop after the gate")
        yield

    monkeypatch.setattr(templates, "_embed_cursor", cursor)
    with pytest.raises(RuntimeError, match="stop after the gate"):
        templates.build_templates("bgl", rebuild=True)
    assert opened == ["yes"], "a rebuild must get past the stored-model check"


def test_filling_in_vectors_with_a_different_model_is_refused(monkeypatch):
    """Without rebuild=True the build only fills NULL embeddings, which would
    leave one table holding two models' vectors."""
    monkeypatch.setattr(
        templates, "embedding_compatibility",
        lambda ds: {"model": "gemini-embedding-2", "declared_dimension": 768,
                    "actual_dimension": 768, "can_embed": True,
                    "stored": {"model": "nomic-embed-text:v1.5"}, "ok": False,
                    "problems": ["the stored vectors were built with 'nomic-embed-text:v1.5' ..."]},
    )
    with pytest.raises(QueryRefused) as caught:
        templates.build_templates("bgl")
    assert caught.value.refusal.code == "embedding_incompatible"


# --- Search degrades rather than ranking on noise --------------------------
def test_search_ignores_vectors_built_by_a_different_model(monkeypatch):
    templates.clear_vector_cache()
    monkeypatch.setattr(
        templates, "templates_status",
        lambda ds: {"exists": True, "embedded": 20352, "model": "nomic-embed-text:v1.5",
                    "configured_model": "gemini-embedding-2",
                    "model_matches_config": False},
    )
    assert templates.has_template_vectors("bgl") is False
    templates.clear_vector_cache()


def test_search_still_uses_vectors_that_predate_model_recording(monkeypatch):
    """Unattributable is not the same as proven wrong: an existing table from
    before this change keeps working rather than being disabled on suspicion."""
    templates.clear_vector_cache()
    monkeypatch.setattr(
        templates, "templates_status",
        lambda ds: {"exists": True, "embedded": 20352, "model": None,
                    "configured_model": "gemini-embedding-2",
                    "model_matches_config": None},
    )
    assert templates.has_template_vectors("bgl") is True
    templates.clear_vector_cache()


def test_search_uses_vectors_from_the_configured_model(monkeypatch):
    templates.clear_vector_cache()
    monkeypatch.setattr(
        templates, "templates_status",
        lambda ds: {"exists": True, "embedded": 43, "model": "gemini-embedding-2",
                    "configured_model": "gemini-embedding-2",
                    "model_matches_config": True},
    )
    assert templates.has_template_vectors("openstack") is True
    templates.clear_vector_cache()


# --- The declared width can follow the model -------------------------------
def test_the_dimension_is_configurable_alongside_the_model(monkeypatch):
    """It was a hard-coded 768 while the configured model's native width is
    3072, so the two could not be made to agree without editing code."""
    import importlib

    monkeypatch.setenv("EMBEDDING_DIMENSION", "3072")
    monkeypatch.setenv("EMBEDDING_MODEL", "gemini-embedding-2")
    from backend.db import config

    reloaded = importlib.reload(config)
    try:
        assert reloaded.EMBEDDING_DIMENSION == 3072
        assert reloaded.EMBEDDING_MODEL == "gemini-embedding-2"
    finally:
        monkeypatch.undo()
        importlib.reload(config)


def test_the_model_is_recorded_so_a_change_is_detectable(monkeypatch):
    """SereneDB cannot say which model produced a stored vector, so the build
    has to write it down."""
    written: list[tuple] = []

    class Cur:
        def execute(self, statement, params=None):
            if params and "gemini" in str(params):
                written.append(params)

    templates._record_embedding_model(Cur(), "bgl")
    assert written and written[0][0] == "bgl"
    assert written[0][1] == templates.EMBEDDING_MODEL
    assert written[0][2] == templates.EMBEDDING_DIMENSION
