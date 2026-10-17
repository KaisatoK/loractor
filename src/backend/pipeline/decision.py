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

# A dataset-wide overview ("what recurs?", "what should we prioritize?") has a
# natural starting point for the team lead and manager: counts over the whole
# dataset. For them an overview counts as scoped; the engineer still needs a
# target. Starting value; validate with routes.sh.
OVERVIEW_THRESHOLD = 0.6

# Roles whose default scope is the whole dataset.
OVERVIEW_ROLES = frozenset({"team_lead", "manager"})

# One long-lived classifier; like models.py it reads TYPESAFE_API_KEY at import.
# No model is passed, so the SDK default selects the release — the gate
# thresholds above were tuned against it, so a model release can move them.
classifier = TypeSafeClassifier()


def _is_log_request(response: ClassifierResponse) -> bool:
    return response.nouls["is_log_request"].noul >= LOG_REQUEST_THRESHOLD


def _names_target(response: ClassifierResponse) -> bool:
    return response.nouls["has_queryable_scope"].noul >= QUERYABLE_SCOPE_THRESHOLD


def _is_overview(response: ClassifierResponse) -> bool:
    overview = response.nouls.get("is_overview_request")
    return overview is not None and overview.noul >= OVERVIEW_THRESHOLD


def _scoped(response: ClassifierResponse, role: str | None) -> bool:
    """A target is named, or an overview is a valid default scope for the role."""
    return _names_target(response) or (role in OVERVIEW_ROLES and _is_overview(response))


def _authorized(response: ClassifierResponse) -> bool:
    return response.nouls["proceed_without_clarification"].noul >= PROCEED_THRESHOLD


def _evidence_route(response: ClassifierResponse) -> str:
    needs_evidence = response.nouls["needs_new_evidence"].noul
    return "find_evidence" if needs_evidence >= NEW_EVIDENCE_THRESHOLD else "direct_answer"


def decide_scope(
    response: ClassifierResponse, clarify_count: int, role: str | None = None
) -> str:
    """Return the scope route using atomic judgments and code-owned policy.

    Non-log requests go to ``general``. Log requests without a queryable target
    are clarified unless the user authorized proceeding, the clarify budget is
    spent, or the request is a dataset-wide overview from a role whose default
    scope is the whole dataset (``OVERVIEW_ROLES``). Past that gate -- including
    a spent clarify budget -- ``needs_new_evidence`` selects between a direct
    answer and evidence retrieval, so a question about the conversation itself
    is never forced into an investigation. The role is applied here, in code;
    the Nouls never see it.
    """
    if not _is_log_request(response):
        return "general"
    if not _scoped(response, role) and not _authorized(response):
        if clarify_count < MAX_CLARIFY_ROUNDS:
            return "clarify"
    return _evidence_route(response)


def scope_assumption(
    response: ClassifierResponse, clarify_count: int, role: str | None = None
) -> bool:
    """Whether the route was reached only because the clarify budget is spent.

    True when the request is a log request with no usable scope and no
    authorization, and asking again is no longer allowed. Code then fills in an
    assumed scope (see ``graph.scope_node``) and states it in the answer.
    """
    return (
        _is_log_request(response)
        and not _scoped(response, role)
        and not _authorized(response)
        and clarify_count >= MAX_CLARIFY_ROUNDS
    )


def clarify_reasons(response: ClassifierResponse, role: str | None = None) -> list[str]:
    """Which gates failed, in plain words, for the clarifier to ask about.

    Derived in code from the same thresholds as ``decide_scope``; the clarifier
    is told why it was called instead of guessing the missing detail.
    """
    reasons: list[str] = []
    if not _names_target(response):
        reasons.append("queryable target missing")
        if role not in OVERVIEW_ROLES and _is_overview(response):
            reasons.append("a dataset-wide overview needs a target for this role")
    if not _authorized(response):
        reasons.append("no authorization to proceed")
    return reasons


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
