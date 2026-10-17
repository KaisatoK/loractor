"""TEMPLATE: live scope-routing tests over whole conversations.

This is a template, not a one-off check: **one test = one scripted conversation**.
Every ``user`` line that carries an expected route is classified LIVE (real
TYPESAFE_API_KEY) in the context of everything before it and asserted. Adding
coverage later is just appending another ``Conversation(...)`` to
``CONVERSATIONS``.

How to run
----------
    RUN_LIVE_ROUTES=1 uv run pytest tests/test_live_scope_routes.py

Each gated run also writes a self-contained HTML report (inline CSS only, no
external resources) to ``.data/scope_routes_report.html`` by default, or to the
path in ``RUN_LIVE_ROUTES_REPORT`` (absolute or relative). It is overwritten every
invocation and reflects only the conversations that ran in this invocation; its
path is printed once per run. A conversation that fails still appears, with its ✗
row and every row up to it. The offline run writes nothing.

This file must be run ALONE (not as part of the full suite): ``tests/conftest.py``
installs dummy provider keys *before* test modules load, and the backend's
``load_dotenv()`` does not override those. When gated, this module calls
``load_dotenv(override=True)`` at import time — before any ``backend.*`` import —
so the real keys from ``.env`` win over the dummy ones. Without
``RUN_LIVE_ROUTES=1`` the live test is skipped and only the offline case-data
sanity test runs; the module is safe to import offline.

Cost & latency
--------------
Each asserted ``user`` turn costs two live calls — ``normalize_node`` (a real
model call) and the scope classifier — plus one more when the turn routes to
``clarify``: the real ``clarify_node`` runs so the report shows its question, and
the code-built options line is asserted from round 2 on. A run needs network
access plus real keys and takes a few minutes. Context-only lines (``assistant``
turns and route-less ``user`` turns) are never classified and cost nothing.

Stochasticity
-------------
The classifier is stochastic on borderline wording, so a failure is not
automatically a regression: the assertion message prints the classifier's raw
numbers (``has_queryable_scope`` and the route probabilities). Re-run before
treating a failure as real.

Notes
-----
- Role influences scope routing only in code: ``decide_scope`` treats a
  dataset-wide overview (``is_overview_request``) as scoped for team_lead and
  manager. The scope request itself carries no role. Each ``Conversation`` sets
  its ``role`` (default engineer).
- ``assistant`` lines are authored context standing in for the pipeline's
  replies; they are not generated, and each must be consistent with the route
  asserted on the turn that produced it.
- Every ``user`` turn that a later assertion depends on carries an expected
  route; a route-less ``user`` turn is only acceptable when nothing downstream
  depends on its route, and the comment must say why.
- The non-latch guarantee is pinned offline (see ``tests/test_decision_nodes.py``),
  not here: a late-conversation underspecified request is a judgement call and
  cannot serve as ground truth for a live assertion.
- The classifier is invoked directly (not via ``scope_node``) so an API failure
  fails the test loudly instead of being swallowed by the node's fallback.
- ``clarify_count`` mirrors ``scope_node``: increment on ``clarify``, otherwise
  reset to 0.
"""

import os
import html
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

RUN_LIVE = os.environ.get("RUN_LIVE_ROUTES") == "1"

if RUN_LIVE:
    from dotenv import load_dotenv

    load_dotenv(override=True)  # replace conftest dummy keys with the real .env ones

import pytest

from backend.pipeline.decision import (
    LOG_REQUEST_THRESHOLD,
    MAX_CLARIFY_ROUNDS,
    NEW_EVIDENCE_THRESHOLD,
    OVERVIEW_THRESHOLD,
    PROCEED_THRESHOLD,
    QUERYABLE_SCOPE_THRESHOLD,
    clarify_reasons,
    classifier,
    decide_scope,
    scope_assumption,
)
from backend.pipeline.graph import clarify_node, normalize_node
from backend.pipeline.prompts import PipelinePrompts, normalized_is_usable
from backend.pipeline.prompts.builders import round_two_question
from backend.pipeline.roles import ROLE_OBJECTIVES
from backend.pipeline.transcript import label_stopped


# --- case data ------------------------------------------------------------

ROUTES = ("clarify", "general", "direct_answer", "find_evidence")

prompts = PipelinePrompts()


@dataclass(frozen=True)
class Conversation:
    id: str
    # ("user", text, expected_route) -> asserted; expected_route may be a tuple
    # when several routes are equally correct (the turn passes on any of them);
    # ("user", text) -> context only
    # (NOT classified); ("assistant", text) -> context only; ("user_stopped", text)
    # -> a user turn whose investigation the user stopped (context only, shown to
    # the classifiers with the stopped label, exactly as ``api.py`` renders it).
    messages: list[tuple[str, str] | tuple[str, str, str | tuple[str, ...]]]
    # The user's role; ``decide_scope`` applies it in code (overview requests
    # are scoped for team_lead/manager). The Nouls never see it.
    role: str = "engineer"


# The shared opening of the "three tables" cases: two vague turns, each clarified.
# Round 1 is a model question (authored here to match the clarifier's rules: list
# every corpus, pick no subset); round 2 is the fixed question built in code.
THREE_TABLES_OPENING = [
    ("user", "What recurring problem do we have across 3 table?", "clarify"),
    (
        "assistant",
        "Which three should I compare? The log corpora are openstack, bgl and OTel "
        "- or do you mean all of them?",
    ),
    ("user", "three log table", "clarify"),
    ("assistant", round_two_question()),
]


CONVERSATIONS = [
    Conversation(
        id="just-do-it-proceeds",
        messages=[
            ("user", "What recurring problem do we have", "clarify"),
            ("assistant", "Which services or time window should I look at?"),
            ("user", "Just do it", "find_evidence"),
        ],
    ),
    Conversation(
        id="task-intent-is-not-authorization",
        messages=[
            ("user", "What recurring problem do we have", "clarify"),
            ("assistant", "Which services or time window should I look at?"),
            ("user", "I want you to do it", "clarify"),
            ("assistant", "I still need a service or time window to narrow this down."),
            ("user", "I want you to do it", "find_evidence"),
        ],
    ),
    Conversation(
        id="interpret-an-earlier-answer",
        messages=[
            ("user", "Which service had the most errors in the last hour?", "find_evidence"),
            (
                "assistant",
                "Checkout had the most errors: 42 between 09:00 and 10:00, "
                "about 3x the next noisiest service.",
            ),
            ("user", "Explain what that number means.", "direct_answer"),
        ],
    ),
    Conversation(
        id="general-then-investigation",
        messages=[
            ("user", "Hi, what can you do?", "general"),
            (
                "assistant",
                "I investigate log data: errors, recurring patterns, and business impact.",
            ),
            ("user", "How many timeout errors were there yesterday?", "find_evidence"),
        ],
    ),
    Conversation(
        id="greetings-are-not-an-investigation",
        messages=[
            ("user", "Yo", "general"),
            (
                "assistant",
                "Hi! I dig through your logs — errors, recurring failures, and "
                "what's driving them. What would you like me to look at?",
            ),
            ("user", "Wassup?", "general"),
        ],
    ),
    # Role-aware scope: the same vague overview is scoped for the team lead and
    # the manager (default scope = whole dataset) but still clarified for the
    # engineer (covered by "just-do-it-proceeds" above).
    Conversation(
        id="team-lead-overview-is-scoped",
        role="team_lead",
        messages=[("user", "What recurring problem do we have", "find_evidence")],
    ),
    Conversation(
        id="manager-priorities-are-scoped",
        role="manager",
        messages=[("user", "What should we prioritize first?", "find_evidence")],
    ),
    Conversation(
        id="manager-specific-but-vague-clarifies",
        role="manager",
        messages=[("user", "Why did it fail?", "clarify")],
    ),
    # Two investigations the user stopped, then a question about the conversation
    # itself: it is not a log request, so it must not restart an investigation.
    # The stopped turns carry no expected route -- they were cut off before any
    # answer, so their own routing is irrelevant; only their stopped label matters.
    Conversation(
        id="recap-after-stopped-questions",
        role="team_lead",
        messages=[
            ("user_stopped", "What recurring problem do we have in the last hour?"),
            ("user_stopped", "Wait, I only want to find service X"),
            ("user", "What was my whole question again?", "general"),
        ],
    ),
    # The "three tables" conversation, as an engineer (an overview needs a target
    # for this role). The reply after round 2 picks from its fixed choices.
    Conversation(
        id="three-tables-named",
        messages=[*THREE_TABLES_OPENING, ("user", "openstack and bgl", "find_evidence")],
    ),
    Conversation(
        id="three-tables-all",
        messages=[*THREE_TABLES_OPENING, ("user", "all of them", "find_evidence")],
    ),
    # "what?" asks about the clarification itself. A general reply and a direct
    # answer are both right; what must not happen is an investigation.
    Conversation(
        id="question-about-the-clarification",
        messages=[
            *THREE_TABLES_OPENING,
            ("user", "what?", ("general", "direct_answer")),
        ],
    ),
]


# --- harness --------------------------------------------------------------


def _render(transcript: list[tuple[str, str]]) -> str:
    """Flatten seen history the way ``api.py::_render_conversation`` does."""
    return "\n".join(f"{role}: {text}" for role, text in transcript)


def _last_assistant(transcript: list[tuple[str, str]]) -> str:
    """The most recent assistant text, used as ``latest_answer``."""
    return next((text for role, text in reversed(transcript) if role == "assistant"), "")


# --- HTML report ----------------------------------------------------------

# Per-conversation records for the current pytest invocation, plus a flag so the
# report path is announced once per run rather than once per conversation.
_REPORT_ENTRIES: list[dict] = []
_REPORT_ANNOUNCED = False

_REPORT_STYLE = """
    body { font-family: system-ui, sans-serif; margin: 2rem; color: #1b1b1b; }
    h1 { font-size: 1.4rem; }
    h2 { font-size: 1.1rem; margin-top: 1.5rem; }
    .meta, .summary, .footer { color: #555; font-size: 0.9rem; }
    table { border-collapse: collapse; margin-top: 0.5rem; }
    th, td { border: 1px solid #ccc; padding: 0.3rem 0.5rem; text-align: left;
             font-size: 0.85rem; vertical-align: top; }
    th { background: #f0f0f0; }
    tr.mismatch td { background: #ffd6d6; }
    tr.context td { color: #777; }
"""


def _report_path() -> Path:
    """``RUN_LIVE_ROUTES_REPORT`` override, else ``.data/scope_routes_report.html``."""
    override = os.environ.get("RUN_LIVE_ROUTES_REPORT")
    if override:
        return Path(override)
    return Path.cwd() / ".data" / "scope_routes_report.html"


def _report_row(row: dict) -> str:
    def cell(value: str) -> str:
        return f"<td>{html.escape(value)}</td>"

    if row["context"]:
        values = [
            str(row["turn"]),
            row["message"],
            "(context)",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
        ]
        return '<tr class="context">' + "".join(cell(value) for value in values) + "</tr>"

    values = [
        str(row["turn"]),
        row["message"],
        row["route"],
        row["expected"],
        "✓" if row["ok"] else "✗",
        f"{row['has_queryable_scope']:.3f} (thr {QUERYABLE_SCOPE_THRESHOLD:.2f})",
        f"{row['is_log_request']:.3f}",
        f"{row['proceed']:.3f}",
        f"{row['needs_new_evidence']:.3f} (thr {NEW_EVIDENCE_THRESHOLD:.2f})",
        f"{row['is_overview_request']:.3f} (thr {OVERVIEW_THRESHOLD:.2f}, role {row['role']})",
        str(row["clarify_count"]),
        "yes" if row["assumed"] else "",
        row["question"],
        row["normalized"],
        row["transcript"],
    ]
    attributes = "" if row["ok"] else ' class="mismatch"'
    return f"<tr{attributes}>" + "".join(cell(value) for value in values) + "</tr>"


def _render_report_html() -> str:
    total_asserted = sum(entry["asserted"] for entry in _REPORT_ENTRIES)
    total_matched = sum(entry["matched"] for entry in _REPORT_ENTRIES)
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    headers = (
        "turn",
        "message",
        "route",
        "expected",
        "ok",
        f"has_queryable_scope (≥ {QUERYABLE_SCOPE_THRESHOLD:.2f})",
        f"is_log_request (≥ {LOG_REQUEST_THRESHOLD:.2f})",
        f"proceed_without_clarification (≥ {PROCEED_THRESHOLD:.2f})",
        f"needs_new_evidence (≥ {NEW_EVIDENCE_THRESHOLD:.2f})",
        f"is_overview_request (≥ {OVERVIEW_THRESHOLD:.2f}, team_lead/manager)",
        "clarify_count",
        "assumed scope",
        "clarifier question",
        "normalized",
        "transcript",
    )

    parts = [
        "<!DOCTYPE html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        "<title>Scope route report</title>",
        f"<style>{_REPORT_STYLE}</style>",
        "</head>",
        "<body>",
        "<h1>Scope route report</h1>",
        f'<p class="meta">Generated {html.escape(generated)}</p>',
        (
            f'<p class="summary">{len(_REPORT_ENTRIES)} conversations, '
            f"{total_matched}/{total_asserted} asserted turns matched</p>"
        ),
    ]

    for entry in _REPORT_ENTRIES:
        parts.append("<section>")
        parts.append(f"<h2>{html.escape(entry['id'])}</h2>")
        parts.append("<table>")
        parts.append(
            "<thead><tr>"
            + "".join(f"<th>{html.escape(label)}</th>" for label in headers)
            + "</tr></thead>"
        )
        parts.append("<tbody>")
        parts.extend(_report_row(row) for row in entry["rows"])
        parts.append("</tbody>")
        parts.append("</table>")
        parts.append(
            f'<p class="footer">{entry["matched"]}/{entry["asserted"]} '
            "asserted turns matched</p>"
        )
        parts.append("</section>")

    parts.append("</body>")
    parts.append("</html>")
    return "\n".join(parts)


def _write_report() -> Path | None:
    """Overwrite the current-invocation HTML report (gated runs only)."""
    global _REPORT_ANNOUNCED
    if not RUN_LIVE:  # the offline run must not create, write, or print anything
        return None
    path = _report_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_render_report_html(), encoding="utf-8")
    if not _REPORT_ANNOUNCED:
        _REPORT_ANNOUNCED = True
        print(f"HTML report: {path.resolve().as_uri()}")
    return path


def run_conversation(conversation: Conversation) -> None:
    """Classify every asserted turn in context, asserting its expected route.

    One state dict is threaded across turns, carrying ``normalized`` forward so
    each turn sees everything before it — what the checkpointer would carry.
    Stops at the first divergence (plain asserts; intended).
    """
    transcript: list[tuple[str, str]] = []
    state: dict = {}
    clarify_count = 0
    asserted_total = 0
    asserted_matched = 0
    rows: list[dict] = []

    try:
        for index, message in enumerate(conversation.messages):
            role, text = message[0], message[1]
            expected = message[2] if len(message) == 3 else None

            assert role in ("user", "assistant", "user_stopped"), (
                f"[{conversation.id}] turn {index}: unknown role {role!r}"
            )
            # A stopped turn is a user turn the agents read with its label.
            transcript.append(
                ("user", label_stopped(text, True))
                if role == "user_stopped"
                else (role, text)
            )

            context_only = role != "user" or expected is None
            if context_only:
                # Context only: extend the transcript, classify nothing.
                rows.append({"turn": index, "message": f"{role}: {text}", "context": True})
                continue

            state.update(
                {
                    "conversation": _render(transcript),
                    "messages": [
                        {"role": item_role, "content": item_text}
                        for item_role, item_text in transcript
                    ],
                    "latest_message": text,
                    "latest_answer": _last_assistant(transcript[:-1]),
                    "role": {
                        "name": conversation.role,
                        "objective": ROLE_OBJECTIVES[conversation.role],
                    },
                    "effort": "Medium",
                    "answer": "",
                }
            )

            state = normalize_node(state)
            assert normalized_is_usable(state["normalized"]), (
                f"[{conversation.id}] turn {index}: normalize output unusable — "
                f"check keys/network (got {state['normalized']!r})"
            )

            response = classifier.invoke(prompts.scope_request(state))
            has_queryable_scope = response.nouls["has_queryable_scope"].noul
            is_log_request = response.nouls["is_log_request"].noul
            proceed = response.nouls["proceed_without_clarification"].noul
            needs_new_evidence = response.nouls["needs_new_evidence"].noul
            overview = response.nouls["is_overview_request"].noul
            route = decide_scope(response, clarify_count, conversation.role)
            assumed = route == "find_evidence" and scope_assumption(
                response, clarify_count, conversation.role
            )

            # A clarify turn also runs the REAL clarifier with what scope_node
            # hands it (reasons and round), so the report shows the question the
            # user would get. Only the code-owned part is asserted: round 2 is
            # exactly the fixed question, round 1 is not. The authored assistant
            # line that follows stays the transcript, so later turns replay.
            question = ""
            options_ok = True
            if route == "clarify":
                round_number = clarify_count + 1
                question = clarify_node(
                    {
                        **state,
                        "clarify_count": round_number,
                        "clarify_reasons": clarify_reasons(response, conversation.role),
                    }
                )["answer"]
                options_ok = (question == round_two_question()) == (round_number >= 2)

            accepted = expected if isinstance(expected, tuple) else (expected,)
            asserted_total += 1
            matched = route in accepted and options_ok
            if matched:
                asserted_matched += 1
            rows.append(
                {
                    "turn": index,
                    "message": f"{role}: {text}",
                    "context": False,
                    "route": route,
                    "expected": " | ".join(accepted),
                    "ok": matched,
                    "has_queryable_scope": has_queryable_scope,
                    "is_log_request": is_log_request,
                    "proceed": proceed,
                    "needs_new_evidence": needs_new_evidence,
                    "is_overview_request": overview,
                    "role": conversation.role,
                    "clarify_count": clarify_count,
                    "assumed": assumed,
                    "question": question,
                    "normalized": json.dumps(state["normalized"], default=str),
                    "transcript": json.dumps(state["messages"], default=str),
                }
            )

            assert route in accepted, (
                f"[{conversation.id}] turn {index} {text!r}: expected {expected!r}, "
                f"got {route!r} (has_queryable_scope={has_queryable_scope:.3f}, "
                f"needs_new_evidence={needs_new_evidence:.3f}, "
                f"is_log_request={is_log_request:.3f}, proceed={proceed:.3f}, "
                f"is_overview_request={overview:.3f}, role={conversation.role}). "
                "Borderline wording can flip run-to-run — re-run before treating "
                "this as a regression."
            )
            assert options_ok, (
                f"[{conversation.id}] turn {index}: clarify round "
                f"{clarify_count + 1} of {MAX_CLARIFY_ROUNDS} must "
                f"{'' if clarify_count + 1 >= 2 else 'not '}be the fixed question; "
                f"got {question!r}"
            )

            clarify_count = clarify_count + 1 if route == "clarify" else 0

    finally:
        # Record whatever ran (a failing conversation still appears, with its ✗
        # row and every row up to it) and refresh the report.
        _REPORT_ENTRIES.append(
            {
                "id": conversation.id,
                "rows": rows,
                "asserted": asserted_total,
                "matched": asserted_matched,
            }
        )
        _write_report()


# --- tests ----------------------------------------------------------------


@pytest.mark.skipif(
    not RUN_LIVE,
    reason=(
        "live API test — run alone with: "
        "RUN_LIVE_ROUTES=1 uv run pytest tests/test_live_scope_routes.py"
    ),
)
@pytest.mark.parametrize("conversation", CONVERSATIONS, ids=lambda c: c.id)
def test_conversation_routes(conversation):
    run_conversation(conversation)


def test_case_data_is_valid():
    """Offline sanity check on the shipped cases (no network)."""
    ids = [conversation.id for conversation in CONVERSATIONS]
    assert len(set(ids)) == len(ids), f"duplicate conversation ids: {ids}"

    for conversation in CONVERSATIONS:
        assert conversation.role in ROLE_OBJECTIVES, (
            f"[{conversation.id}] unknown role {conversation.role!r}"
        )
        asserted = 0
        for message in conversation.messages:
            assert len(message) in (2, 3), (
                f"[{conversation.id}] malformed message {message!r}"
            )
            role = message[0]
            text = message[1]
            assert role in ("user", "assistant", "user_stopped"), (
                f"[{conversation.id}] unknown role {role!r}"
            )
            assert isinstance(text, str), f"[{conversation.id}] non-str text {text!r}"
            if len(message) == 3:
                assert role == "user", (
                    f"[{conversation.id}] expected route on a non-user message {message!r}"
                )
                accepted = message[2] if isinstance(message[2], tuple) else (message[2],)
                assert accepted and all(route in ROUTES for route in accepted), (
                    f"[{conversation.id}] unknown route {message[2]!r}"
                )
                asserted += 1

        assert asserted >= 1, (
            f"[{conversation.id}] has no asserted user turn"
        )


def test_round_two_authored_line_is_the_fixed_question():
    # The authored round-2 assistant line must be exactly what clarify_node
    # emits; otherwise later turns replay a stale prompt.
    assert THREE_TABLES_OPENING[3] == ("assistant", round_two_question())
