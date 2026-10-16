"""Merged type-ahead across the local corpora, and the chat box's selector gate.

Offline: the private per-dataset lookup (``_auto_complete_dataset``) is stubbed,
so these exercise the merge, the ranking and the corpus choice of the public
``auto_complete`` rather than SQL (which ``test_query_layer.py`` covers per
dataset).
"""

from __future__ import annotations

import importlib

import pytest

from backend.db.datasets import DatasetKind

# ``backend.db`` re-exports the ``auto_complete`` *function* under the module's
# name, so the module itself is fetched by its dotted path.
ac = importlib.import_module("backend.db.auto_complete")


@pytest.fixture
def per_dataset(monkeypatch):
    """Install canned per-dataset results; record which corpora were asked."""
    calls: list[str] = []

    def install(results: dict[str, object]):
        def fake(query, dataset=None):
            calls.append(dataset)
            outcome = results.get(dataset, [])
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        monkeypatch.setattr(ac, "_auto_complete_dataset", fake)
        return calls

    return install


def test_the_single_dataset_lookup_is_private():
    import backend.db as db

    assert "auto_complete_local" not in dir(ac)
    assert not hasattr(db, "_auto_complete_dataset")
    assert db.auto_complete is ac.auto_complete
    assert "_auto_complete_dataset" not in db.__all__


def test_only_local_tables_are_merged():
    ids = {ds.id for ds in ac.local_datasets()}
    assert ids == {"openstack", "bgl"}
    assert all(ds.kind is DatasetKind.NATIVE_TABLE for ds in ac.local_datasets())


def test_remote_corpora_are_never_queried(per_dataset):
    calls = per_dataset({})
    ac.auto_complete("@nova")
    assert set(calls) == {"openstack", "bgl"}
    assert "otel_s3" not in calls and "otel_slice" not in calls


def test_identical_values_are_merged_with_summed_counts(per_dataset):
    per_dataset(
        {
            "openstack": [{"suggestion": "shared.component", "count": 3}],
            "bgl": [
                {"suggestion": "shared.component", "count": 4},
                {"suggestion": "KERNEL", "count": 1},
            ],
        }
    )
    result = ac.auto_complete("@comp")
    assert {"suggestion": "shared.component", "count": 7} in result
    assert {"suggestion": "KERNEL", "count": 1} in result


def test_template_selector_returns_templates_from_both_corpora(per_dataset):
    per_dataset(
        {
            "openstack": [{"suggestion": "Took <*> seconds to spawn", "count": 9}],
            "bgl": [{"suggestion": "instruction cache parity error", "count": 2}],
        }
    )
    suggestions = {row["suggestion"] for row in ac.auto_complete("^t")}
    assert suggestions == {"Took <*> seconds to spawn", "instruction cache parity error"}


def test_line_suggestions_keep_their_dataset_and_are_not_merged(per_dataset):
    per_dataset(
        {
            "openstack": [{"suggestion": "timeout", "line_id": 42}],
            "bgl": [{"suggestion": "timeout", "line_id": 42}],
        }
    )
    result = ac.auto_complete("$timeout")
    assert {"suggestion": "timeout", "line_id": 42, "dataset": "openstack"} in result
    assert {"suggestion": "timeout", "line_id": 42, "dataset": "bgl"} in result


def test_a_missing_table_is_skipped(per_dataset, caplog):
    per_dataset(
        {
            "openstack": RuntimeError('relation "bgl" does not exist'),
            "bgl": [{"suggestion": "KERNEL", "count": 5}],
        }
    )
    with caplog.at_level("WARNING"):
        result = ac.auto_complete("@K")
    assert result == [{"suggestion": "KERNEL", "count": 5}]
    assert "openstack" in caplog.text


def test_starts_with_ranks_before_popularity_and_the_list_is_capped(per_dataset):
    per_dataset(
        {
            "openstack": [
                {"suggestion": f"x.nova.{i}", "count": 100 + i} for i in range(6)
            ],
            "bgl": [{"suggestion": "nova.api", "count": 1}],
        }
    )
    result = ac.auto_complete("@nova")
    assert result[0]["suggestion"] == "nova.api"
    assert len(result) == ac._LIMIT


def test_case_modifier_ranks_case_matches_first(per_dataset):
    per_dataset(
        {
            "openstack": [{"suggestion": "nova.api", "count": 50}],
            "bgl": [{"suggestion": "Nova.api", "count": 1}],
        }
    )
    assert ac.auto_complete("@!Nova")[0]["suggestion"] == "Nova.api"
    assert ac.auto_complete("@Nova")[0]["suggestion"] == "nova.api"


def test_an_empty_query_suggests_nothing(per_dataset):
    calls = per_dataset({})
    assert ac.auto_complete("") == []
    assert calls == []


# --- Chat box gate -----------------------------------------------------------
def _chat_box_module():
    # ``ui.chat_box`` re-exports the ``chat_box`` *function* under the module's
    # name, so the module itself is fetched by its dotted path.
    import importlib

    return importlib.import_module("ui.chat_box.chat_box")


def _chat_box(monkeypatch):
    chat_box = _chat_box_module()

    seen: list[str] = []
    monkeypatch.setattr(
        chat_box.chat_client,
        "auto_complete",
        lambda token, word: seen.append(word) or [{"suggestion": word, "count": 1}],
    )
    return chat_box, seen


def test_chat_box_selectors_are_service_template_and_content(monkeypatch):
    chat_box, seen = _chat_box(monkeypatch)
    assert chat_box._SELECTORS == "@^$"
    for word in ("@nova", "^Took", "$timeout", "@!nova"):
        assert chat_box._suggestions("t", word)
    assert seen == ["@nova", "^Took", "$timeout", "@!nova"]


def test_chat_box_does_not_look_up_a_bang_word(monkeypatch):
    chat_box, seen = _chat_box(monkeypatch)
    assert chat_box._suggestions("t", "!257") == []
    assert chat_box._suggestions("t", "#E25") == []
    assert seen == []


def test_chat_box_inserts_a_dataset_qualified_line_reference():
    """The JS has no test harness, so its insertion text is checked as source."""
    from pathlib import Path

    chat_box = _chat_box_module()

    js = (Path(chat_box.__file__).parent / "chat_box.js").read_text(encoding="utf-8")
    assert 'const SELECTORS = "@^$";' in js
    assert "`Line ID: ${s.line_id} (${s.dataset})`" in js
