"""Offline tests for the code-owned TypeSafe decision policies."""

import os

os.environ.setdefault("TYPESAFE_API_KEY", "test-key")

from langchain_typesafe import ClassifierResponse, NoulAnswer
from backend.pipeline.decision import (
    ANSWER_OK_THRESHOLD,
    EVIDENCE_REVISION_THRESHOLD,
    LOG_REQUEST_THRESHOLD,
    MAX_CLARIFY_ROUNDS,
    NEW_EVIDENCE_THRESHOLD,
    OVERVIEW_THRESHOLD,
    PROCEED_THRESHOLD,
    QUERYABLE_SCOPE_THRESHOLD,
    clarify_reasons,
    decide_confidence,
    decide_scope,
    scope_assumption,
)


def _response(answers):
    return ClassifierResponse(model="test-model", answers=answers)


def _noul(value):
    return NoulAnswer(type="noul", noul=value)


def _scope_response(
    has_queryable_scope,
    needs_new_evidence=1.0,
    proceed=0.0,
    is_log_request=1.0,
    is_overview_request=None,
):
    answers = {
        "is_log_request": _noul(is_log_request),
        "has_queryable_scope": _noul(has_queryable_scope),
        "proceed_without_clarification": _noul(proceed),
        "needs_new_evidence": _noul(needs_new_evidence),
    }
    if is_overview_request is not None:
        answers["is_overview_request"] = _noul(is_overview_request)
    return _response(answers)


def _conf_response(
    *,
    evidence_relevant=1.0,
    evidence_gap_remains=0.0,
    gap_closable_by_untried_tool=0.0,
    answer_grounded=1.0,
    answer_fits_role=1.0,
    answer_complete=1.0,
    worth=0.0,
):
    return _response(
        {
            "evidence_relevant": _noul(evidence_relevant),
            "evidence_gap_remains": _noul(evidence_gap_remains),
            "gap_closable_by_untried_tool": _noul(gap_closable_by_untried_tool),
            "answer_grounded": _noul(answer_grounded),
            "answer_fits_role": _noul(answer_fits_role),
            "answer_complete": _noul(answer_complete),
            "worth_follow_up": _noul(worth),
        }
    )


# --- decide_scope ---------------------------------------------------------


def test_scope_clarifies_when_no_queryable_scope_and_budget_remains():
    assert decide_scope(_scope_response(0.0), clarify_count=0) == "clarify"


def test_scope_answers_directly_when_clarify_budget_spent_and_no_evidence_needed():
    # "what?" after two clarifications is a question about the conversation:
    # a spent budget must not force it into an investigation.
    response = _scope_response(0.0, needs_new_evidence=NEW_EVIDENCE_THRESHOLD - 0.01)
    assert decide_scope(response, clarify_count=MAX_CLARIFY_ROUNDS) == "direct_answer"
    assert scope_assumption(response, MAX_CLARIFY_ROUNDS)


def test_scope_finds_evidence_when_clarify_budget_spent_and_evidence_needed():
    response = _scope_response(0.0, needs_new_evidence=NEW_EVIDENCE_THRESHOLD)
    assert decide_scope(response, clarify_count=MAX_CLARIFY_ROUNDS) == "find_evidence"
    assert scope_assumption(response, MAX_CLARIFY_ROUNDS)


def test_scope_assumption_is_false_when_budget_remains():
    assert not scope_assumption(_scope_response(0.0), MAX_CLARIFY_ROUNDS - 1)


def test_scope_assumption_is_false_when_scoped_authorized_overview_or_general():
    spent = MAX_CLARIFY_ROUNDS
    assert not scope_assumption(_scope_response(QUERYABLE_SCOPE_THRESHOLD), spent)
    assert not scope_assumption(_scope_response(0.0, proceed=PROCEED_THRESHOLD), spent)
    overview = _scope_response(0.0, is_overview_request=OVERVIEW_THRESHOLD)
    assert not scope_assumption(overview, spent, role="team_lead")
    assert scope_assumption(overview, spent, role="engineer")
    assert not scope_assumption(_scope_response(0.0, is_log_request=0.0), spent)


def test_clarify_reasons_name_the_failed_gates():
    response = _scope_response(0.0, proceed=0.0, is_overview_request=0.9)
    assert clarify_reasons(response, "engineer") == [
        "queryable target missing",
        "a dataset-wide overview needs a target for this role",
        "no authorization to proceed",
    ]
    # For a role whose default scope is the whole dataset the overview is not a gap.
    assert clarify_reasons(response, "team_lead") == [
        "queryable target missing",
        "no authorization to proceed",
    ]
    named = _scope_response(QUERYABLE_SCOPE_THRESHOLD, proceed=PROCEED_THRESHOLD)
    assert clarify_reasons(named, "engineer") == []


def test_scope_routes_direct_answer_at_new_evidence_boundary_below():
    response = _scope_response(
        QUERYABLE_SCOPE_THRESHOLD,
        needs_new_evidence=NEW_EVIDENCE_THRESHOLD - 0.01,
    )
    assert decide_scope(response, clarify_count=0) == "direct_answer"


def test_scope_routes_find_evidence_at_new_evidence_boundary():
    response = _scope_response(
        QUERYABLE_SCOPE_THRESHOLD,
        needs_new_evidence=NEW_EVIDENCE_THRESHOLD,
    )
    assert decide_scope(response, clarify_count=0) == "find_evidence"


def test_scope_log_request_does_not_use_general_route_label():
    response = _scope_response(QUERYABLE_SCOPE_THRESHOLD, needs_new_evidence=0.0)
    assert decide_scope(response, clarify_count=0) == "direct_answer"


def test_scope_proceeds_to_evidence_when_authorized():
    response = _scope_response(0.0, needs_new_evidence=0.9, proceed=0.9)
    assert decide_scope(response, clarify_count=0) == "find_evidence"


def test_scope_authorization_bypasses_spent_clarify_cap():
    response = _scope_response(0.0, needs_new_evidence=0.0, proceed=0.9)
    assert decide_scope(response, clarify_count=MAX_CLARIFY_ROUNDS) == "direct_answer"


def test_scope_not_authorized_still_clarifies_when_budget_remains():
    response = _scope_response(0.0, proceed=0.1)
    assert decide_scope(response, clarify_count=0) == "clarify"


def test_scope_proceed_at_threshold_fires():
    response = _scope_response(0.0, needs_new_evidence=1.0, proceed=PROCEED_THRESHOLD)
    assert decide_scope(response, clarify_count=0) == "find_evidence"


def test_scope_proceed_below_threshold_clarifies():
    response = _scope_response(
        0.0, proceed=PROCEED_THRESHOLD - 0.01, needs_new_evidence=1.0
    )
    assert decide_scope(response, clarify_count=0) == "clarify"


def test_scope_routes_general_when_not_a_log_request():
    response = _scope_response(0.0, is_log_request=0.0)
    assert decide_scope(response, clarify_count=0) == "general"


def test_scope_not_a_log_request_routes_general_even_when_budget_spent():
    response = _scope_response(0.0, is_log_request=0.1)
    assert decide_scope(response, clarify_count=MAX_CLARIFY_ROUNDS) == "general"


def test_queryable_scope_threshold_exceeds_log_request_threshold():
    assert LOG_REQUEST_THRESHOLD < QUERYABLE_SCOPE_THRESHOLD


# --- role-aware overview scope -------------------------------------------


def test_overview_is_scoped_for_team_lead_and_manager():
    response = _scope_response(0.0, is_overview_request=0.9)
    for role in ("team_lead", "manager"):
        assert decide_scope(response, clarify_count=0, role=role) == "find_evidence"


def test_overview_still_clarifies_for_engineer():
    response = _scope_response(0.0, is_overview_request=0.9)
    assert decide_scope(response, clarify_count=0, role="engineer") == "clarify"
    assert decide_scope(response, clarify_count=0) == "clarify"


def test_overview_threshold_boundary():
    at = _scope_response(0.0, is_overview_request=OVERVIEW_THRESHOLD)
    below = _scope_response(0.0, is_overview_request=OVERVIEW_THRESHOLD - 0.01)
    assert decide_scope(at, clarify_count=0, role="manager") == "find_evidence"
    assert decide_scope(below, clarify_count=0, role="manager") == "clarify"


def test_specific_unscoped_question_clarifies_for_manager():
    # "Why did it fail?" with no target is not an overview: manager clarifies too.
    response = _scope_response(0.0, is_overview_request=0.1)
    assert decide_scope(response, clarify_count=0, role="manager") == "clarify"


def test_overview_never_overrides_general_route():
    response = _scope_response(0.0, is_log_request=0.0, is_overview_request=0.99)
    assert decide_scope(response, clarify_count=0, role="team_lead") == "general"


def test_missing_overview_noul_falls_back_to_clarify():
    response = _scope_response(0.0)  # no is_overview_request answer
    assert decide_scope(response, clarify_count=0, role="team_lead") == "clarify"


# --- decide_confidence ----------------------------------------------------


def test_confidence_finds_evidence_for_closable_gap():
    response = _conf_response(
        evidence_gap_remains=1.0,
        gap_closable_by_untried_tool=EVIDENCE_REVISION_THRESHOLD,
    )
    result = decide_confidence(response, "Medium", revision_count=0)
    assert result["next_state"] == "find_evidence"
    assert result["revision_reason"] == "missing_evidence"


def test_confidence_finds_evidence_for_irrelevant_report():
    response = _conf_response(
        evidence_relevant=1.0 - EVIDENCE_REVISION_THRESHOLD,
        gap_closable_by_untried_tool=1.0,
    )
    result = decide_confidence(response, "Medium", revision_count=0)
    assert result["next_state"] == "find_evidence"
    assert result["revision_reason"] == "irrelevant_evidence"


def test_confidence_ends_when_gap_is_not_closable():
    response = _conf_response(
        evidence_gap_remains=1.0,
        gap_closable_by_untried_tool=0.0,
    )
    result = decide_confidence(response, "Medium", revision_count=0)
    assert result["next_state"] == "end"
    assert result["revision_reason"] == "none"


def test_confidence_revises_unsupported_answer():
    result = decide_confidence(
        _conf_response(answer_grounded=ANSWER_OK_THRESHOLD - 0.01),
        "High",
        revision_count=0,
    )
    assert result["next_state"] == "answer"
    assert result["revision_reason"] == "unsupported_answer"


def test_confidence_revises_role_mismatch():
    result = decide_confidence(
        _conf_response(answer_fits_role=ANSWER_OK_THRESHOLD - 0.01),
        "High",
        revision_count=0,
    )
    assert result["next_state"] == "answer"
    assert result["revision_reason"] == "role_mismatch"


def test_confidence_revises_incomplete_answer():
    result = decide_confidence(
        _conf_response(answer_complete=ANSWER_OK_THRESHOLD - 0.01),
        "High",
        revision_count=0,
    )
    assert result["next_state"] == "answer"
    assert result["revision_reason"] == "incomplete_answer"


def test_confidence_forced_end_when_revision_budget_exhausted():
    result = decide_confidence(
        _conf_response(evidence_gap_remains=1.0, gap_closable_by_untried_tool=1.0),
        "Medium",
        revision_count=1,
    )
    assert result["next_state"] == "end"
    assert result["revision_reason"] == "none"


def test_confidence_low_effort_always_ends():
    result = decide_confidence(
        _conf_response(evidence_gap_remains=1.0, gap_closable_by_untried_tool=1.0),
        "Low",
        revision_count=0,
    )
    assert result["next_state"] == "end"
    assert result["revision_reason"] == "none"


def test_confidence_propagates_worth_follow_up_and_signals():
    result = decide_confidence(_conf_response(worth=0.9), "Medium", revision_count=0)
    assert result["worth_follow_up"] == 0.9
    assert result["signals"]["answer_grounded"] == 1.0
    assert set(result) == {"next_state", "revision_reason", "signals", "worth_follow_up"}


# --- scope_node authorization non-persistence -----------------------------


def test_scope_node_persists_no_authorization_signal(monkeypatch):
    from backend.pipeline import graph

    response = _scope_response(0.0, needs_new_evidence=1.0, proceed=0.95)

    class _StubClassifier:
        def invoke(self, request):
            return response

    monkeypatch.setattr(graph, "classifier", _StubClassifier())
    state = {
        "conversation": (
            "user: What recurring problem do we have\n"
            "assistant: Which services or time window should I look at?\n"
            "user: Just do it"
        ),
        "normalized": {
            "normalized_question": "What recurring problem do we have",
            "intent": "understand recurring problems",
            "context": {},
            "ambiguities": ["which service", "which time window"],
        },
        "latest_message": "Just do it",
        "latest_answer": "Which services or time window should I look at?",
        "messages": [],
    }

    out = graph.scope_node(state)

    assert out["scope"] == "find_evidence"
    # Only routing bookkeeping is added; nothing records the authorization itself.
    assert set(out) - set(state) == {
        "scope",
        "clarify_count",
        "scope_signals",
        "clarify_reasons",
        "assumed_scope",
    }
    assert out["assumed_scope"] is None


def _stub_scope(monkeypatch, response):
    from backend.pipeline import graph

    class _StubClassifier:
        def invoke(self, request):
            return response

    monkeypatch.setattr(graph, "classifier", _StubClassifier())
    return graph


def _vague_state(clarify_count, role="engineer"):
    return {
        "conversation": "user: What recurring problem do we have across 3 table?",
        "normalized": "What recurring problem do we have across 3 tables?",
        "latest_message": "three log table",
        "latest_answer": "Which three tables should I look at?",
        "messages": [],
        "role": {"name": role, "objective": "x"},
        "clarify_count": clarify_count,
    }


def test_scope_node_keeps_raw_signals_and_reasons_when_clarifying(monkeypatch):
    response = _scope_response(0.21, proceed=0.08, is_overview_request=0.82)
    graph = _stub_scope(monkeypatch, response)
    out = graph.scope_node(_vague_state(0))
    assert out["scope"] == "clarify" and out["clarify_count"] == 1
    assert out["scope_signals"]["has_queryable_scope"] == 0.21
    assert out["scope_signals"]["is_overview_request"] == 0.82
    assert out["clarify_reasons"] == clarify_reasons(response, "engineer")
    assert out["assumed_scope"] is None


def test_scope_node_assumes_every_local_corpus_when_the_budget_is_spent(monkeypatch):
    from backend.db.datasets import local_datasets

    graph = _stub_scope(monkeypatch, _scope_response(0.2, needs_new_evidence=0.9))
    out = graph.scope_node(_vague_state(MAX_CLARIFY_ROUNDS))
    assert out["scope"] == "find_evidence"
    assert out["assumed_scope"] == [ds.id for ds in local_datasets()]
    assert out["clarify_count"] == 0


def test_scope_node_answers_directly_without_assumption_when_no_evidence_needed(
    monkeypatch,
):
    graph = _stub_scope(monkeypatch, _scope_response(0.2, needs_new_evidence=0.19))
    out = graph.scope_node(_vague_state(MAX_CLARIFY_ROUNDS))
    assert out["scope"] == "direct_answer"
    assert out["assumed_scope"] is None


def test_scope_node_does_not_carry_an_assumption_into_the_next_turn(monkeypatch):
    graph = _stub_scope(monkeypatch, _scope_response(QUERYABLE_SCOPE_THRESHOLD))
    state = {**_vague_state(0), "assumed_scope": ["openstack", "bgl"]}
    assert graph.scope_node(state)["assumed_scope"] is None
