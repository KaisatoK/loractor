"""Deterministic policies composed from TypeSafe (Jev) judgments."""

from langchain_typesafe import ClassifierResponse, TypeSafeClassifier

from backend.pipeline.effort import get_effort_spec

# The "is a target named?" boundary for the scope gate. Below it no queryable
# target (dataset, service, entity id, event template or error/message text,
# severity, time window -- see ``QUERYABLE_TARGETS``) was found, so the
# product asks — biased toward asking (clarification is cheap; a vague run isn't).
QUERYABLE_SCOPE_THRESHOLD = 0.6

# When a log request is ambiguous about whether retrieval is needed, favor retrieval:
# an unnecessary read is recoverable, while answering without needed evidence is not.
# This is a starting threshold validated by the live route harness.
NEW_EVIDENCE_THRESHOLD = 0.4

# Noul probabilities are probabilities of yes, not separate confidence values.
FOLLOW_UP_THRESHOLD = 0.75
PROCEED_THRESHOLD = 0.5

# Consecutive clarification turns before the pipeline stops asking and instead
# investigates best-effort so the user still gets an answer.
MAX_CLARIFY_ROUNDS = 2

# Below this the newest message is judged not a log request, so ``general``
# answers it conversationally. A false ``true`` costs one recoverable nudge; a
# false ``false`` misroutes a real run, so the middle band falls to the gate.
LOG_REQUEST_THRESHOLD = 0.3

# Confidence revision thresholds. A revision is worthwhile only when both the
# relevant defect and a useful action have meaningful probability. Starting values.
EVIDENCE_REVISION_THRESHOLD = 0.6
ANSWER_OK_THRESHOLD = 0.45

# One long-lived classifier; like models.py it reads TYPESAFE_API_KEY at import.
# No model is passed, so the SDK default selects the release — the gate
# thresholds above were tuned against it, so a model release can move them.
classifier = TypeSafeClassifier()


def decide_scope(response: ClassifierResponse, clarify_count: int) -> str:
    """Return the scope route using atomic judgments and code-owned policy.

    Non-log requests go to ``general``. Log requests without a queryable target
    are clarified unless the user authorized proceeding or the clarify budget is
    spent. Once that gate is passed, ``needs_new_evidence`` selects between a
    direct answer and evidence retrieval.
    """
    if response.nouls["is_log_request"].noul < LOG_REQUEST_THRESHOLD:
        return "general"

    scoped = response.nouls["has_queryable_scope"].noul >= QUERYABLE_SCOPE_THRESHOLD
    if not scoped:
        authorized = (
            response.nouls["proceed_without_clarification"].noul >= PROCEED_THRESHOLD
        )
        if not authorized:
            return "clarify" if clarify_count < MAX_CLARIFY_ROUNDS else "find_evidence"

    needs_evidence = response.nouls["needs_new_evidence"].noul
    return "find_evidence" if needs_evidence >= NEW_EVIDENCE_THRESHOLD else "direct_answer"


def _signal(response: ClassifierResponse, name: str) -> float:
    """Read a Noul probability by id, keeping response access in one place."""
    return response.nouls[name].noul


def decide_confidence(
    response: ClassifierResponse, effort: str | None, revision_count: int
) -> dict:
    """Compose atomic confidence judgments into the bounded revision policy.

    The classifier only judges independent properties. Code owns the workflow:
    evidence retrieval is preferred when a material, closable gap or irrelevant
    report is likely; otherwise answer quality is repaired when one quality signal
    is weak; spent revision budgets always end the run.
    """
    signals = {
        "evidence_relevant": _signal(response, "evidence_relevant"),
        "evidence_gap_remains": _signal(response, "evidence_gap_remains"),
        "gap_closable_by_untried_tool": _signal(
            response, "gap_closable_by_untried_tool"
        ),
        "answer_grounded": _signal(response, "answer_grounded"),
        "answer_fits_role": _signal(response, "answer_fits_role"),
        "answer_complete": _signal(response, "answer_complete"),
    }
    worth_follow_up = _signal(response, "worth_follow_up")
    spec = get_effort_spec(effort)

    if revision_count >= spec.max_iterations:
        next_state = "end"
        revision_reason = "none"
    elif (
        signals["evidence_gap_remains"]
        * signals["gap_closable_by_untried_tool"]
        >= EVIDENCE_REVISION_THRESHOLD
    ):
        next_state = "find_evidence"
        revision_reason = "missing_evidence"
    elif (
        (1.0 - signals["evidence_relevant"])
        * signals["gap_closable_by_untried_tool"]
        >= EVIDENCE_REVISION_THRESHOLD
    ):
        next_state = "find_evidence"
        revision_reason = "irrelevant_evidence"
    else:
        weakest = min(
            (signals["answer_grounded"], "unsupported_answer"),
            (signals["answer_fits_role"], "role_mismatch"),
            (signals["answer_complete"], "incomplete_answer"),
            key=lambda item: item[0],
        )
        if weakest[0] < ANSWER_OK_THRESHOLD:
            next_state = "answer"
            revision_reason = weakest[1]
        else:
            next_state = "end"
            revision_reason = "none"

    return {
        "next_state": next_state,
        "revision_reason": revision_reason,
        "signals": signals,
        "worth_follow_up": worth_follow_up,
    }
