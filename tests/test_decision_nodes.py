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


def test_confidence_honors_find_evidence_even_when_pass_high():
    response = _conf_response(1.0, "find_evidence", "missing_evidence")
    result = decide_confidence(response, "Medium", revision_count=0)
    assert result["next_state"] == "find_evidence"
    assert result["revision_reason"] == "missing_evidence"
    assert result["pass_probability"] == 1.0


def test_confidence_honors_answer_even_when_pass_high():
    response = _conf_response(1.0, "answer", "unsupported_answer")
    result = decide_confidence(response, "High", revision_count=0)
    assert result["next_state"] == "answer"
    assert result["revision_reason"] == "unsupported_answer"


def test_confidence_ends_when_next_state_is_end():
    response = _conf_response(0.1, "end", "missing_evidence")
    result = decide_confidence(response, "Medium", revision_count=0)
    assert result["next_state"] == "end"
    assert result["revision_reason"] == "none"


def test_confidence_forced_end_when_revision_budget_exhausted():
    # Medium max_iterations == 1; at the cap the route is end regardless of choice.
    response = _conf_response(0.0, "find_evidence", "missing_evidence")
    result = decide_confidence(response, "Medium", revision_count=1)
    assert result["next_state"] == "end"
    assert result["revision_reason"] == "none"


def test_confidence_low_effort_always_ends():
    # Low max_iterations == 0, so even revision_count 0 is already at the cap.
    response = _conf_response(0.0, "find_evidence", "missing_evidence")
    result = decide_confidence(response, "Low", revision_count=0)
    assert result["next_state"] == "end"
    assert result["revision_reason"] == "none"


def test_confidence_propagates_worth_follow_up():
    response = _conf_response(0.5, "find_evidence", "missing_evidence", worth=0.9)
    result = decide_confidence(response, "Medium", revision_count=0)
    assert result["worth_follow_up"] == 0.9


def test_confidence_probabilities_have_expected_keys():
    response = _conf_response(0.5, "find_evidence", "missing_evidence")
    result = decide_confidence(response, "Medium", revision_count=0)
    assert set(result["probabilities"]) == {"find_evidence", "answer", "end"}
    assert set(result) == {
        "next_state",
        "pass_probability",
        "probabilities",
        "revision_reason",
        "worth_follow_up",
    }
