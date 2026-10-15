from langchain_typesafe import ClassifierResponse, TypeSafeClassifier

from backend.pipeline.effort import get_effort_spec

# Route to a clarification question when the model is at least this sure that the
# request cannot be answered or investigated without more information from the user.
CLARIFY_THRESHOLD = 0.64

# Only append the answer agent's drafted follow-up question when the model is at
# least this sure it would materially improve the next turn.
FOLLOW_UP_THRESHOLD = 0.75

# The authorization head is a yes/no ("has the user told us to proceed anyway?"),
# not a calibrated ambiguity gate, so it only needs to clear even odds.
PROCEED_THRESHOLD = 0.5

# Consecutive clarification turns before the pipeline stops asking and instead
# investigates best-effort so the user still gets an answer.
MAX_CLARIFY_ROUNDS = 2

# One long-lived classifier. Like the chat models in models.py, it reads
# TYPESAFE_API_KEY at import time.
classifier = TypeSafeClassifier()

_SCOPE_ROUTES = ("clarify", "general", "direct_answer", "find_evidence")


def _route(response: ClassifierResponse) -> str:
    """The route choice, falling back to direct_answer for an unknown label."""
    route = response.choices["route"].choice
    return route if route in _SCOPE_ROUTES else "direct_answer"


def decide_scope(response: ClassifierResponse, clarify_count: int) -> str:
    """Return one of clarify/general/direct_answer/find_evidence.

    Two-stage decision: gate on ambiguity, then pick the action. When the request
    is ambiguous the classifier may still yield to the route if the user has
    authorized proceeding without the missing detail; authorization is checked
    before the retry cap so it decides even once the cap is spent. Otherwise the
    request is clarified until the budget runs out, after which the pipeline
    forces a best-effort investigation.
    """
    needs = response.nouls["needs_clarification"].noul
    if needs >= CLARIFY_THRESHOLD:
        if response.nouls["proceed_without_clarification"].noul >= PROCEED_THRESHOLD:
            return _route(response)
        return "clarify" if clarify_count < MAX_CLARIFY_ROUNDS else "find_evidence"
    return _route(response)


def decide_confidence(
    response: ClassifierResponse, effort: str | None, revision_count: int
) -> dict:
    """Return the confidence route plus the follow-up signal.

    Keys: next_state (find_evidence|answer|end), pass_probability,
    probabilities (per next_state), revision_reason, worth_follow_up.

    Routing depends only on the revision budget and the classifier's
    ``next_state`` choice: ``pass`` is diagnostic/logging-only and never moves
    the route. The effort spec caps the revision loop; once the budget is spent
    the run always ends.
    """
    spec = get_effort_spec(effort)
    pass_probability = response.nouls["pass"].noul
    next_answer = response.choices["next_state"]
    reason = response.choices["revision_reason"]

    if revision_count >= spec.max_iterations:
        next_state = "end"
    else:
        next_state = next_answer.choice

    revision_reason = "none" if next_state == "end" else reason.choice

    return {
        "next_state": next_state,
        "pass_probability": pass_probability,
        "probabilities": {
            "find_evidence": next_answer.probabilities.get("find_evidence", 0.0),
            "answer": next_answer.probabilities.get("answer", 0.0),
            "end": next_answer.probabilities.get("end", 0.0),
        },
        "revision_reason": revision_reason,
        "worth_follow_up": response.nouls["worth_follow_up"].noul,
    }
