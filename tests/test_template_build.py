"""The template embedding build must always terminate.

This file exists because it did not. Two real defects, both found by running it:

* the first version embedded and inserted in one statement, re-selecting
  whatever was "not yet present" each round. A NULL template is never equal to
  itself, so those rows were re-selected forever; a run reported **38,371 of
  20,353** templates embedded before it was killed.
* ``UPDATE SET embedding = ai_embed(...)`` reports rows affected even when the
  function yields NULL, so a single template the embedding service cannot handle
  would be re-selected forever at a steady batch per round -- indistinguishable
  from healthy progress if the loop trusts rows-affected.

A 20,352-template corpus takes ~28 minutes to embed, so "watch it and see" is
not a viable check. These tests drive the loop against a stub cursor and assert
it stops, in every failure mode, without touching a database or an embedding
service.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from sql_render import render  # noqa: E402

from backend.db import templates  # noqa: E402


class StubCursor:
    """Enough of a cursor to drive ``build_templates`` offline.

    ``embeddable`` decides which template texts the pretend embedding service
    can handle; anything else stays NULL, which is the condition that used to
    spin forever.
    """

    def __init__(self, pending: int, *, embeddable=lambda text: True):
        self.pending = pending
        self.embeddable = embeddable
        self.rows: dict[int, dict] = {}
        self.rowcount = 0
        self.update_calls = 0
        self._result: list[dict] = []
        self.statements: list[str] = []

    # -- helpers -----------------------------------------------------------
    def _unembedded_ids(self) -> list[int]:
        return sorted(
            tid
            for tid, row in self.rows.items()
            if row["embedding"] is None and row["template_text"] is not None
        )

    def execute(self, query, params=None):
        # The offline renderer, because Composable.as_string needs a live
        # connection for identifier quoting and the point here is to run
        # without one.
        text = query if isinstance(query, str) else render(query)
        text = re.sub(r"\s+", " ", text).strip()
        self.statements.append(text)
        self.rowcount = 0
        self._result = []

        if "information_schema.tables" in text:
            self._result = [{"n": 1}]
        elif "FROM _pending" in text and "count(*)" in text:
            self._result = [{"n": self.pending}]
        elif "max(template_id)" in text:
            self._result = [{"m": max(self.rows, default=0)}]
        elif text.startswith("INSERT INTO"):
            start = max(self.rows, default=0)
            for offset in range(self.pending):
                tid = start + offset + 1
                self.rows[tid] = {
                    "template_text": f"template {offset}",
                    "row_count": offset + 1,
                    "embedding": None,
                }
            self.rowcount = self.pending
        elif text.startswith("UPDATE"):
            self.update_calls += 1
            for tid in self._unembedded_ids()[: templates.EMBED_BATCH]:
                row = self.rows[tid]
                self.rowcount += 1
                if self.embeddable(row["template_text"]):
                    row["embedding"] = [0.0]
        elif "embedding IS NULL" in text and "count(*)" in text:
            self._result = [{"n": len(self._unembedded_ids())}]
        elif "count(embedding)" in text or "rows_covered" in text:
            embedded = sum(1 for r in self.rows.values() if r["embedding"])
            self._result = [
                {
                    "templates": len(self.rows),
                    "embedded": embedded,
                    "rows_covered": sum(r["row_count"] for r in self.rows.values()),
                }
            ]
        elif "pg_total_relation_size" in text:
            self._result = [{"b": 1024}]
        elif "pg_database_size" in text:
            self._result = [{"b": 1024}]

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return self._result

class StubContext:
    def __init__(self, cursor):
        self.cursor = cursor

    def __enter__(self):
        return self.cursor

    def __exit__(self, *exc):
        return False


@pytest.fixture
def stub(monkeypatch):
    def install(cursor):
        monkeypatch.setattr(
            templates, "_embed_cursor", lambda: StubContext(cursor)
        )
        templates.clear_vector_cache()
        return cursor

    return install


# --- The happy path still works --------------------------------------------
def test_a_normal_build_embeds_everything_and_stops(stub):
    cursor = stub(StubCursor(pending=450))
    report = templates.build_templates("otel_slice", rebuild=True)

    assert report["embedded"] == 450
    assert report["unembedded"] == 0
    assert report["incomplete_reason"] is None


def test_the_number_of_rounds_matches_the_batch_size(stub):
    """A tight bound, so a regression that re-embeds shows up here."""
    cursor = stub(StubCursor(pending=templates.EMBED_BATCH * 3))
    templates.build_templates("otel_slice", rebuild=True)
    assert cursor.update_calls == 3


def test_nothing_is_embedded_twice(stub):
    cursor = stub(StubCursor(pending=300))
    report = templates.build_templates("otel_slice", rebuild=True)
    assert report["embedded"] == len(cursor.rows) == 300


# --- The failure that spun forever ------------------------------------------
def test_a_template_the_service_cannot_embed_does_not_spin(stub, monkeypatch):
    """The exact shape of the original bug.

    One template always comes back NULL. The update still reports rows
    affected, so a loop trusting rows-affected never exits.
    """
    monkeypatch.setattr(templates, "EMBED_BATCH", 10)
    poison = "template 7"
    cursor = stub(StubCursor(pending=50, embeddable=lambda text: text != poison))
    report = templates.build_templates("otel_slice", rebuild=True)

    # Stops with work outstanding, and says why.
    assert report["unembedded"] >= 1
    assert report["incomplete_reason"] is not None
    # The progress guard is what catches this: rows-affected looked healthy.
    assert "progress" in report["incomplete_reason"]
    # Bounded. Without a guard this loop did not end.
    assert cursor.update_calls <= 7


def test_an_entirely_unembeddable_corpus_stops_after_one_round(stub, monkeypatch):
    monkeypatch.setattr(templates, "EMBED_BATCH", 25)
    cursor = stub(StubCursor(pending=100, embeddable=lambda _t: False))
    report = templates.build_templates("otel_slice", rebuild=True)

    assert report["embedded"] == 0
    assert report["unembedded"] == 100
    assert report["incomplete_reason"] is not None
    assert cursor.update_calls == 1


def test_the_round_ceiling_is_derived_from_the_work_outstanding(stub):
    """Even if the progress check were removed, iterations stay bounded."""
    cursor = stub(StubCursor(pending=1000))
    templates.build_templates("otel_slice", rebuild=True)
    assert cursor.update_calls <= 1000 // templates.EMBED_BATCH + 2


def test_an_incomplete_build_is_reported_not_hidden(stub, monkeypatch):
    """A partially embedded corpus must not look complete.

    Semantic search over 90% of templates that claims to cover everything would
    make "nothing found" read as evidence of absence.
    """
    monkeypatch.setattr(templates, "EMBED_BATCH", 5)
    stub(StubCursor(pending=40, embeddable=lambda t: t != "template 0"))
    report = templates.build_templates("otel_slice", rebuild=True)
    assert report["incomplete_reason"]
    assert report["unembedded"] > 0


# --- Resume safety ----------------------------------------------------------
def test_ids_continue_from_the_existing_maximum(stub):
    """``row_number()`` alone restarts at 1, so a resumed build would issue ids
    that already exist and the update would target the wrong rows."""
    cursor = StubCursor(pending=10)
    cursor.rows = {
        1: {"template_text": "old", "row_count": 1, "embedding": [0.0]},
        2: {"template_text": "older", "row_count": 1, "embedding": [0.0]},
    }
    stub(cursor)
    templates.build_templates("otel_slice")

    assert len(cursor.rows) == 12
    assert sorted(cursor.rows) == list(range(1, 13))


def test_a_build_with_nothing_outstanding_issues_no_updates(stub):
    cursor = StubCursor(pending=0)
    stub(cursor)
    report = templates.build_templates("otel_slice")

    assert cursor.update_calls == 0
    assert report["embedded"] == 0
    assert report["incomplete_reason"] is None


# --- Guards -----------------------------------------------------------------
def test_an_implausible_template_count_is_refused(stub):
    """A corpus whose templating collapsed nothing would embed for hours."""
    from backend.db.errors import QueryRefused

    stub(StubCursor(pending=templates.MAX_TEMPLATES + 1))
    with pytest.raises(QueryRefused) as excinfo:
        templates.build_templates("otel_slice", rebuild=True)
    assert excinfo.value.refusal.code == "too_many_templates"


def test_progress_is_reported_as_it_goes(stub):
    """A 28-minute build has to be observable while it runs."""
    seen: list[tuple[int, int]] = []
    stub(StubCursor(pending=templates.EMBED_BATCH * 2))
    templates.build_templates(
        "otel_slice", rebuild=True, progress=lambda done, total: seen.append((done, total))
    )
    assert seen
    assert seen[-1][0] == templates.EMBED_BATCH * 2
