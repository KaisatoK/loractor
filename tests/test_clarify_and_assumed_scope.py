"""Context-aware clarification, the assumed scope and the no-evidence answer.

Offline: the clarifier model, the answer model and the registry are stubbed, so
these pin only what code owns -- what the clarifier is shown, the options line
built from the registry, the assumed-scope line and the answer rules chosen.
"""

import os

os.environ.setdefault("TYPESAFE_API_KEY", "test-key")

from types import SimpleNamespace

from backend.db.datasets import catalog, local_datasets
from backend.pipeline import graph
from backend.pipeline.contracts import AnswerOutput
from backend.pipeline.decision import MAX_CLARIFY_ROUNDS
from backend.pipeline.playbooks import role_answer_rules
from backend.pipeline.prompts import builders
from backend.pipeline.prompts.builders import (
    NO_EVIDENCE_RULE,
    PipelinePrompts,
    assumed_scope_line,
    clarify_choices,
    clarify_options,
    round_two_question,
)

REASONS = ["queryable target missing", "no authorization to proceed"]


def _clarify_state(round_number, role="engineer"):
    return {
        "role": {"name": role, "objective": "Find the root cause."},
        "normalized": "What recurring problem do we have across 3 tables?",
        "conversation": "user: What recurring problem do we have across 3 table?",
        "clarify_count": round_number,
        "clarify_reasons": REASONS,
    }


class _StubClarifier:
    def __init__(self):
        self.prompts = []

    def invoke(self, payload):
        self.prompts.append(payload["messages"][0]["content"])
        return {"messages": [SimpleNamespace(text="Which datasets do you mean?")]}


# --- what the clarifier is shown -------------------------------------------


def test_clarify_prompt_carries_catalog_reasons_and_round():
    prompt = PipelinePrompts().clarify(_clarify_state(1), max_rounds=MAX_CLARIFY_ROUNDS)
    for dataset_id in catalog():
        assert f'"{dataset_id}"' in prompt
    for reason in REASONS:
        assert f"- {reason}" in prompt
    assert f"clarify round: 1 of {MAX_CLARIFY_ROUNDS}" in prompt
    # The same queryable context the scope gate judges against.
    assert '"queryable_targets"' in prompt and '"tool_catalog"' in prompt


def test_clarify_node_asks_the_model_in_round_one(monkeypatch):
    stub = _StubClarifier()
    monkeypatch.setattr(graph, "clarify_agent", stub)
    out = graph.clarify_node(_clarify_state(1))
    assert out["answer"] == "Which datasets do you mean?"
    assert len(stub.prompts) == 1


def test_clarify_node_round_two_is_the_fixed_code_question(monkeypatch):
    stub = _StubClarifier()
    monkeypatch.setattr(graph, "clarify_agent", stub)
    out = graph.clarify_node(_clarify_state(2))
    assert out["answer"] == round_two_question()
    assert out["answer"].endswith(clarify_options())
    assert stub.prompts == []  # no model call: nothing to reword or guess


def test_choices_are_one_per_corpus_covering_every_dataset():
    from backend.db.datasets import corpora

    groups = corpora()
    assert clarify_choices() == [
        ids[0] if len(ids) == 1 else name for name, ids in groups.items()
    ]
    covered = [dataset_id for ids in groups.values() for dataset_id in ids]
    assert sorted(covered) == sorted(catalog())
    # The OTel views are one choice: a user is never asked to pick a view.
    assert "OTel" in clarify_choices()
    assert "otel_s3" not in clarify_choices() and "otel_slice" not in clarify_choices()


def test_catalog_tells_both_judges_which_datasets_share_a_corpus():
    from backend.db.datasets import corpora

    for name, ids in corpora().items():
        for dataset_id in ids:
            assert catalog()[dataset_id]["corpus"] == name


def test_options_come_from_the_registry_not_from_constants(monkeypatch):
    fake = {"Acme": ("acme",), "Remote": ("remote_a", "remote_b")}
    monkeypatch.setattr("backend.db.datasets.corpora", lambda: fake)
    assert clarify_options() == "Pick one: acme, Remote, or all of them."


# --- assumed scope -----------------------------------------------------------


def test_assumed_scope_line_names_the_searched_and_the_skipped_datasets():
    local = [ds.id for ds in local_datasets()]
    line = assumed_scope_line(local)
    assert line.startswith(f"Assumed scope: {', '.join(local)} (all local corpora")
    for dataset_id in set(catalog()) - set(local):
        assert dataset_id in line
    assert assumed_scope_line(None) == "" and assumed_scope_line([]) == ""


def _evidence_state(assumed):
    return {
        "role": {"name": "engineer", "objective": "o"},
        "effort": "Medium",
        "normalized": "recurring problems",
        "assumed_scope": assumed,
    }


def test_evidence_prompt_restricts_an_assumed_scope_only():
    assumed = PipelinePrompts().find_evidence(_evidence_state(["openstack", "bgl"]))
    normal = PipelinePrompts().find_evidence(_evidence_state(None))
    assert "<assumed scope>" in assumed
    assert "Investigate only these datasets: openstack, bgl." in assumed
    assert "<assumed scope>" not in normal


def _answer(text="Answer text."):
    return AnswerOutput.model_validate(
        {"blocks": [{"type": "text", "text": text}], "findings": [], "recommendations": []}
    )


def _answer_state(assumed=None, tool_results=None, tool_calls=None):
    return {
        "role": {"name": "engineer", "objective": "o"},
        "effort": "Medium",
        "normalized": "q",
        "evidence_report": {},
        "tool_results": tool_results or [],
        "tool_calls": tool_calls or [],
        "assumed_scope": assumed,
    }


def test_answer_starts_with_the_assumption_exactly_once_across_revisions(monkeypatch):
    monkeypatch.setattr(graph, "generate_answer", lambda prompt: _answer())
    state = _answer_state(assumed=["openstack", "bgl"])
    first = graph.answer_node(state)
    # A confidence revision re-runs the answer node on the returned state.
    second = graph.answer_node(first)
    line = assumed_scope_line(["openstack", "bgl"])
    for out in (first, second):
        assert out["answer"].startswith(line)
        assert out["answer"].count("Assumed scope:") == 1


def test_answer_has_no_assumption_line_when_the_user_chose_the_scope(monkeypatch):
    monkeypatch.setattr(graph, "generate_answer", lambda prompt: _answer())
    assert graph.answer_node(_answer_state())["answer"] == "Answer text."


# --- no-evidence answer mode -------------------------------------------------


def test_answer_prompt_without_evidence_drops_the_role_evidence_sections():
    prompt = PipelinePrompts().answer(_answer_state())
    assert NO_EVIDENCE_RULE in prompt
    assert "## Answer must contain" not in prompt
    assert "## Question this role asks" in prompt and "## Never" in prompt


def test_answer_prompt_with_evidence_keeps_every_role_section():
    record = {"id": "R1", "tool_name": "search_logs_tool", "rows": [], "row_count": 0}
    with_results = PipelinePrompts().answer(_answer_state(tool_results=[record]))
    failed_call = {"call_id": "c1", "tool_name": "search_logs_tool", "status": "failure"}
    with_calls = PipelinePrompts().answer(_answer_state(tool_calls=[failed_call]))
    for prompt in (with_results, with_calls):
        assert NO_EVIDENCE_RULE not in prompt
        assert "## Answer must contain" in prompt


def test_role_answer_rules_returns_the_requested_sections_in_order():
    full = role_answer_rules("engineer")
    subset = role_answer_rules("engineer", ("Question this role asks", "Never"))
    assert subset.startswith("## Question this role asks")
    assert "## Never" in subset and "## Answer must contain" not in subset
    assert "## Answer must contain" in full
