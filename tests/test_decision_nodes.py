"""Offline unit tests for the pure TypeSafe decision helpers.

These exercise ``decide_scope`` and ``decide_confidence`` in
``backend.pipeline.decision`` without any network access: importing the module
constructs a ``TypeSafeClassifier`` that only needs a key to exist, so a dummy
key is set before import and every response is built by hand.
"""

import os

os.environ.setdefault("TYPESAFE_API_KEY", "test-key")

from langchain_typesafe import ChoiceAnswer, ClassifierResponse, NoulAnswer
from backend.pipeline.decision import (
    CLARIFY_THRESHOLD,
    MAX_CLARIFY_ROUNDS,
    decide_confidence,
    decide_scope,
)


def _response(answers):
    return ClassifierResponse(model="test-model", answers=answers)


def _noul(value):
    return NoulAnswer(type="noul", noul=value)


def _choice(label, confidence=0.8):
    return ChoiceAnswer(
        type="choice",
        choice=label,
        probabilities={label: confidence, "other": round(1 - confidence, 2)},
        confidence=confidence,
    )


def _scope_response(needs, route_label):
    return _response(
        {
            "needs_clarification": _noul(needs),
            "route": _choice(route_label),
        }
    )


def _conf_response(pass_p, next_state, reason, worth=0.0):
    return _response(
        {
            "pass": _noul(pass_p),
            "next_state": _choice(next_state),
            "revision_reason": _choice(reason),
            "worth_follow_up": _noul(worth),
        }
    )


# --- decide_scope ---------------------------------------------------------


def test_scope_clarifies_when_needs_meets_threshold_and_budget_remains():
    response = _scope_response(CLARIFY_THRESHOLD, "general")
    assert decide_scope(response, clarify_count=0) == "clarify"


def test_scope_finds_evidence_when_clarify_budget_spent():
    response = _scope_response(CLARIFY_THRESHOLD, "general")
    assert decide_scope(response, clarify_count=MAX_CLARIFY_ROUNDS) == "find_evidence"


def test_scope_routes_general_when_no_clarification_needed():
    response = _scope_response(0.0, "general")
    assert decide_scope(response, clarify_count=0) == "general"


def test_scope_routes_direct_answer():
    response = _scope_response(0.0, "direct_answer")
    assert decide_scope(response, clarify_count=0) == "direct_answer"


def test_scope_routes_find_evidence():
    response = _scope_response(0.0, "find_evidence")
    assert decide_scope(response, clarify_count=0) == "find_evidence"


def test_scope_unknown_route_falls_back_to_direct_answer():
    response = _scope_response(0.0, "mystery")
    assert decide_scope(response, clarify_count=0) == "direct_answer"


# --- decide_confidence ----------------------------------------------------


def test_confidence_ends_when_pass_above_threshold():
    response = _conf_response(0.8, "find_evidence", "missing_evidence")
    result = decide_confidence(response, "Medium", revision_count=0)
    assert result["next_state"] == "end"
    assert result["revision_reason"] == "none"


def test_confidence_revises_when_pass_below_threshold():
    response = _conf_response(0.5, "find_evidence", "missing_evidence")
    result = decide_confidence(response, "Medium", revision_count=0)
    assert result["next_state"] == "find_evidence"
    assert result["revision_reason"] == "missing_evidence"


def test_confidence_ends_when_revision_budget_exhausted():
    response = _conf_response(0.5, "find_evidence", "missing_evidence")
    result = decide_confidence(response, "Medium", revision_count=10)
    assert result["next_state"] == "end"


def test_confidence_propagates_worth_follow_up():
    response = _conf_response(0.5, "find_evidence", "missing_evidence", worth=0.9)
    result = decide_confidence(response, "Medium", revision_count=0)
    assert result["worth_follow_up"] == 0.9


def test_confidence_endpoint_depends_on_effort_threshold():
    response = _conf_response(0.6, "find_evidence", "missing_evidence")

    low = decide_confidence(response, "Low", revision_count=0)
    assert low["next_state"] == "end"

    brutal = decide_confidence(response, "Brutal", revision_count=0)
    assert brutal["next_state"] == "find_evidence"


def test_confidence_probabilities_have_expected_keys():
    response = _conf_response(0.5, "find_evidence", "missing_evidence")
    result = decide_confidence(response, "Medium", revision_count=0)
    assert set(result["probabilities"]) == {"find_evidence", "answer", "end"}
