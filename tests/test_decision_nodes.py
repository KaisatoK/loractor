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
    LOG_REQUEST_THRESHOLD,
    MAX_CLARIFY_ROUNDS,
    PROCEED_THRESHOLD,
    QUERYABLE_SCOPE_THRESHOLD,
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


def _scope_response(has_queryable_scope, route_label, proceed=0.0, is_log_request=1.0):
    return _response(
        {
            "is_log_request": _noul(is_log_request),
            "has_queryable_scope": _noul(has_queryable_scope),
            "proceed_without_clarification": _noul(proceed),
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


def test_scope_clarifies_when_no_queryable_scope_and_budget_remains():
    response = _scope_response(0.0, "general")
    assert decide_scope(response, clarify_count=0) == "clarify"


def test_scope_finds_evidence_when_clarify_budget_spent():
    response = _scope_response(0.0, "general")
    assert decide_scope(response, clarify_count=MAX_CLARIFY_ROUNDS) == "find_evidence"


def test_scope_routes_route_when_queryable_scope_present():
    response = _scope_response(QUERYABLE_SCOPE_THRESHOLD, "general")
    assert decide_scope(response, clarify_count=0) == "general"


def test_scope_routes_direct_answer():
    response = _scope_response(1.0, "direct_answer")
    assert decide_scope(response, clarify_count=0) == "direct_answer"


def test_scope_routes_find_evidence():
    response = _scope_response(1.0, "find_evidence")
    assert decide_scope(response, clarify_count=0) == "find_evidence"


def test_scope_unknown_route_falls_back_to_direct_answer():
    response = _scope_response(1.0, "mystery")
    assert decide_scope(response, clarify_count=0) == "direct_answer"


def test_scope_proceeds_to_route_when_authorized():
    response = _scope_response(0.0, "find_evidence", proceed=0.9)
    assert decide_scope(response, clarify_count=0) == "find_evidence"


def test_scope_authorization_bypasses_spent_clarify_cap():
    # Authorization is checked before the cap, so the route decides at the cap.
    response = _scope_response(0.0, "direct_answer", proceed=0.9)
    assert (
        decide_scope(response, clarify_count=MAX_CLARIFY_ROUNDS) == "direct_answer"
    )


def test_scope_not_authorized_still_clarifies_when_budget_remains():
    response = _scope_response(0.0, "find_evidence", proceed=0.1)
    assert decide_scope(response, clarify_count=0) == "clarify"


def test_scope_authorized_route_general_is_honored():
    response = _scope_response(0.0, "general", proceed=0.9)
    assert decide_scope(response, clarify_count=0) == "general"


def test_scope_proceed_at_threshold_fires():
    response = _scope_response(0.0, "find_evidence", proceed=PROCEED_THRESHOLD)
    assert decide_scope(response, clarify_count=0) == "find_evidence"


def test_scope_proceed_below_threshold_clarifies():
    response = _scope_response(
        0.0, "find_evidence", proceed=PROCEED_THRESHOLD - 0.01
    )
    assert decide_scope(response, clarify_count=0) == "clarify"


def test_scope_routes_general_when_not_a_log_request():
    # A content-free greeting is not an investigation, so the log-request gate
    # wins over an unscoped request and an unrelated route choice.
    response = _scope_response(0.0, "find_evidence", is_log_request=0.0)
    assert decide_scope(response, clarify_count=0) == "general"


def test_scope_not_a_log_request_routes_general_even_when_clarify_budget_spent():
    # The gate precedes the spent-cap fallback, so it yields general, not
    # find_evidence, even at the clarification cap.
    response = _scope_response(0.0, "find_evidence", is_log_request=0.1)
    assert decide_scope(response, clarify_count=MAX_CLARIFY_ROUNDS) == "general"


def test_scope_still_clarifies_when_log_request_and_no_queryable_scope():
    # A real but unscoped log request keeps the existing behavior.
    response = _scope_response(0.0, "general", is_log_request=0.9)
    assert decide_scope(response, clarify_count=0) == "clarify"


def test_queryable_scope_threshold_exceeds_log_request_threshold():
    # The log-request gate must fire first: a confident "not a log request"
    # routes general before the target gate is ever consulted.
    assert LOG_REQUEST_THRESHOLD < QUERYABLE_SCOPE_THRESHOLD


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


# --- scope_node authorization non-persistence -----------------------------


def test_scope_node_persists_no_authorization_signal(monkeypatch):
    """The authorization verdict must not survive the turn.

    ``decide_scope`` reads ``proceed_without_clarification`` from the current
    classifier response only; ``scope_node`` must not write it (or any derived
    flag) into state, or a later turn would inherit the previous turn's
    authorization. The non-latch guarantee otherwise rests on a live judgement
    call, which is why it is pinned structurally here instead.
    """
    from backend.pipeline import graph

    response = _scope_response(0.0, "find_evidence", proceed=0.95)

    class _StubClassifier:
        def invoke(self, request):
            return response

    monkeypatch.setattr(graph, "classifier", _StubClassifier())

    # No clarify_count: the node's ``state.get("clarify_count", 0)`` default is
    # the turn's starting count, so the node adds it (and scope) itself.
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
    }

    out = graph.scope_node(state)

    assert out["scope"] == "find_evidence"
    assert set(out) - set(state) == {"scope", "clarify_count"}
