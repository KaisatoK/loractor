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


def _codes(report) -> set[str]:
    return {problem["code"] for problem in report["problems"]}


def _message(report, code) -> str:
    return next(p["message"] for p in report["problems"] if p["code"] == code)


def _cursor_with(monkeypatch, *, dimension=None, embed_error=None, stored=None,
                 stored_width=None):
    """Install an ``_embed_cursor`` whose embedding and build record are fixed."""

    class Cur:
        def execute(self, statement, params=None):
            text = str(statement)
            if "ai_embed" in text:
                if embed_error:
                    raise RuntimeError(embed_error)
                self._row = {"emb": [0.0] * dimension}
            elif "embedding IS NOT NULL" in text:
                self._row = (
                    {"embedding": [0.0] * stored_width} if stored_width else None
                )
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
    assert _codes(report) == {"cannot_embed", "missing_secret"}
    # The hint names the secret the calls actually use, whatever it is called.
    from backend.db.config import EMBEDDING_SECRET

    assert f"'{EMBEDDING_SECRET}'" in _message(report, "missing_secret")
    assert "container" in _message(report, "missing_secret")


def test_a_width_mismatch_is_detected_and_names_the_value_to_set(monkeypatch):
    """gemini-embedding-2 returns 3072 natively while the table stores 768; the
    cast would truncate instead of refusing."""
    monkeypatch.setattr(templates, "EMBEDDING_DIMENSION", 768)
    _cursor_with(monkeypatch, dimension=3072)
    report = templates.embedding_compatibility("bgl")
    assert report["can_embed"] is True
    assert report["actual_dimension"] == 3072
    assert report["ok"] is False
    assert "EMBEDDING_DIMENSION=3072" in _message(report, "declared_dimension")


def test_vectors_from_another_model_are_reported_as_meaningless(monkeypatch):
    monkeypatch.setattr(templates, "EMBEDDING_MODEL", "gemini-embedding-2")
    monkeypatch.setattr(templates, "EMBEDDING_DIMENSION", 768)
    _cursor_with(monkeypatch, dimension=768,
                 stored={"model": "nomic-embed-text:v1.5", "dimension": 768,
                         "built_at": "2026-01-01 00:00:00+00"})
    report = templates.embedding_compatibility("bgl")
    assert report["ok"] is False
    assert "stale_model" in _codes(report)
    assert "meaningless" in _message(report, "stale_model")


def test_a_matching_model_and_width_is_compatible(monkeypatch):
    monkeypatch.setattr(templates, "EMBEDDING_MODEL", "gemini-embedding-2")
    monkeypatch.setattr(templates, "EMBEDDING_DIMENSION", 768)
    _cursor_with(monkeypatch, dimension=768, stored_width=768,
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
                    "ok": False,
                    "problems": [{"code": "cannot_embed",
                                  "message": "the database cannot embed"}]},
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
                    "problems": [{"code": "stale_model",
                                  "message": "built with another model"}]},
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
                    "problems": [{"code": "stale_model",
                                  "message": "built with another model"}]},
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
            if params and templates.EMBEDDING_MODEL in str(params):
                written.append(params)

    templates._record_embedding_model(Cur(), "bgl")
    assert written and written[0][0] == "bgl"
    assert written[0][1] == templates.EMBEDDING_MODEL
    assert written[0][2] == templates.EMBEDDING_DIMENSION


# --- The stored width is measured, not assumed -----------------------------
def test_vectors_of_another_width_are_caught_even_with_no_model_recorded(monkeypatch):
    """The live state after the model change: 20,352 vectors, no recorded
    model, and 768 floats each against a 3072-dimension model. The column is
    declared FLOAT[n] but SereneDB reports its type only as ARRAY, so the width
    has to come from a row."""
    monkeypatch.setattr(templates, "EMBEDDING_DIMENSION", 3072)
    _cursor_with(monkeypatch, dimension=3072, stored=None, stored_width=768)
    report = templates.embedding_compatibility("bgl")
    assert report["can_embed"] is True
    assert report["actual_dimension"] == 3072
    assert report["stored_dimension"] == 768
    assert report["ok"] is False
    assert "stale_dimension" in _codes(report)
    assert "rebuild" in _message(report, "stale_dimension")


def test_a_table_with_no_vectors_yet_has_no_width_problem(monkeypatch):
    monkeypatch.setattr(templates, "EMBEDDING_DIMENSION", 3072)
    _cursor_with(monkeypatch, dimension=3072, stored=None, stored_width=None)
    report = templates.embedding_compatibility("bgl")
    assert report["stored_dimension"] is None
    assert report["ok"] is True


def test_a_rebuild_may_replace_vectors_of_another_width(monkeypatch):
    """Both staleness problems are cleared by dropping the table, so neither
    may block the rebuild that fixes them."""
    assert templates.STALE_CODES == {"stale_model", "stale_dimension"}
    monkeypatch.setattr(
        templates, "embedding_compatibility",
        lambda ds: {"model": "gemini-embedding-2", "declared_dimension": 3072,
                    "actual_dimension": 3072, "can_embed": True, "stored": None,
                    "stored_dimension": 768, "ok": False,
                    "problems": [{"code": "stale_dimension",
                                  "message": "768-wide vectors"}]},
    )
    reached: list[str] = []

    @contextlib.contextmanager
    def cursor():
        reached.append("yes")
        raise RuntimeError("stop after the gate")
        yield

    monkeypatch.setattr(templates, "_embed_cursor", cursor)
    with pytest.raises(RuntimeError, match="stop after the gate"):
        templates.build_templates("bgl", rebuild=True)
    assert reached == ["yes"]

    # ... and the same problem refuses a fill-in build, which would leave one
    # table holding vectors of two widths.
    with pytest.raises(QueryRefused):
        templates.build_templates("bgl")


def test_search_falls_back_when_the_stored_width_differs(monkeypatch):
    """A 3072-wide query vector cannot be compared with 768-wide stored ones,
    and this is caught even though no model was recorded."""
    templates.clear_vector_cache()
    monkeypatch.setattr(
        templates, "templates_status",
        lambda ds: {"exists": True, "embedded": 20352, "model": None,
                    "model_matches_config": None, "stored_dimension": 768,
                    "dimension_matches_config": False,
                    "configured_dimension": 3072},
    )
    assert templates.has_template_vectors("bgl") is False
    templates.clear_vector_cache()


# --- The secret is named by configuration, not hard-coded ------------------
def test_every_embed_call_names_the_configured_secret():
    """The calls hard-coded 'local_ai'; the secret is now 'gemini', and the
    name belongs in configuration either way."""
    import re
    from pathlib import Path

    from backend.db.config import EMBEDDING_SECRET

    assert EMBEDDING_SECRET
    for name in ("templates.py", "query.py"):
        source = Path("src/backend/db") / name
        text = source.read_text(encoding="utf-8")
        assert "'local_ai'" not in text, name
        # Real call sites only: they interpolate the model, unlike the prose
        # mentions of ``ai_embed(...)`` in comments.
        calls = re.findall(r"ai_embed\([^)]*\{model\}[^)]*\)", text)
        assert calls, f"{name}: no ai_embed call found"
        for call in calls:
            assert "{secret}" in call, f"{name}: {call}"


# --- Completeness and expiry of the vector cache ---------------------------
def test_a_partial_template_table_searches_lexically(monkeypatch):
    templates.clear_vector_cache()
    monkeypatch.setattr(
        templates, "templates_status",
        lambda ds: {"exists": True, "templates": 86, "embedded": 40, "complete": False,
                    "model_matches_config": True, "dimension_matches_config": True},
    )
    assert templates.has_template_vectors("openstack") is False
    templates.clear_vector_cache()


def test_the_vector_cache_expires_so_a_cli_build_is_noticed(monkeypatch):
    templates.clear_vector_cache()
    states = iter([
        {"exists": False},
        {"exists": True, "templates": 2, "embedded": 2, "complete": True,
         "model_matches_config": True, "dimension_matches_config": True},
    ])
    monkeypatch.setattr(templates, "templates_status", lambda ds: next(states))
    clock = [1000.0]
    monkeypatch.setattr(templates.time, "monotonic", lambda: clock[0])
    assert templates.has_template_vectors("openstack") is False
    clock[0] += templates.VECTOR_CACHE_TTL_S - 1
    assert templates.has_template_vectors("openstack") is False  # still cached
    clock[0] += 2
    assert templates.has_template_vectors("openstack") is True   # re-probed
    templates.clear_vector_cache()
