"""Offline tests for TypeSafe scope and confidence request payloads."""

import os

os.environ.setdefault("TYPESAFE_API_KEY", "test-key")

from backend.pipeline import tools
from backend.pipeline.prompts.builders import (
    PIPELINE_MISSION,
    QUERYABLE_TARGETS,
    TOOL_CATALOG,
    PipelinePrompts,
)


def _state():
    return {
        "conversation": "user: Why did checkout errors spike this morning?",
        "messages": [
            {"role": "user", "content": "Why did checkout errors spike this morning?"}
        ],
        "normalized": "Why did checkout errors spike this morning?",
        "latest_message": "Why did checkout errors spike this morning?",
        "latest_answer": "Checkout errors rose 3x after 09:00.",
        "role": {
            "name": "engineer",
            "objective": "Find the root cause of a specific error.",
        },
        "effort": "Medium",
        "evidence_report": [{"hypotheses": []}],
        "answer": "draft answer",
        "tool_results": [
            {
                "id": "R1",
                "tool_name": "search_logs_tool",
                "arguments": {"query_text": "timeout"},
                "row_count": 7,
                "rows": [],
            },
            {
                "id": "R2",
                "tool_name": "count_by_category_tool",
                "arguments": {"type": "service"},
                "row_count": 3,
                "rows": [],
            },
        ],
    }


def test_tool_catalog_matches_available_tools():
    assert set(TOOL_CATALOG) == set(tools.__all__)


def test_scope_request_contains_only_code_owned_scope_context():
    payload = PipelinePrompts().scope_request(_state())["state"]
    assert payload["tool_catalog"] == TOOL_CATALOG
    assert payload["mission"] == PIPELINE_MISSION
    assert payload["queryable_targets"] == QUERYABLE_TARGETS
    assert payload["transcript"] == _state()["messages"]
    assert "database_schema" not in payload
    assert "normalized" not in payload
    assert "clarify_count" not in payload


def test_scope_request_has_atomic_questions():
    questions = PipelinePrompts().scope_request(_state())["questions"]
    assert set(questions) == {
        "is_log_request",
        "has_queryable_scope",
        "proceed_without_clarification",
        "needs_new_evidence",
        "is_overview_request",
    }
    assert "route" not in questions


def test_scope_request_never_sends_the_role():
    # The role is applied in code (decide_scope), never inside a Noul.
    request = PipelinePrompts().scope_request(_state())
    assert "role" not in request["state"]
    assert "engineer" not in str(request["questions"]["is_overview_request"].instructions)


def test_scope_request_carries_latest_message_and_answer():
    payload = PipelinePrompts().scope_request(_state())["state"]
    assert payload["latest_message"] == "Why did checkout errors spike this morning?"
    assert payload["latest_answer"] == "Checkout errors rose 3x after 09:00."


def test_confidence_request_has_atomic_questions_and_trimmed_state():
    request = PipelinePrompts().confidence_request(_state())
    payload = request["state"]
    assert payload["tool_catalog"] == TOOL_CATALOG
    assert payload["attempted_tools"] == [
        {
            "tool_name": "search_logs_tool",
            "arguments": {"query_text": "timeout"},
            "row_count": 7,
        },
        {
            "tool_name": "count_by_category_tool",
            "arguments": {"type": "service"},
            "row_count": 3,
        },
    ]
    assert set(payload) == {
        "request",
        "role",
        "evidence_report",
        "answer",
        "tool_catalog",
        "attempted_tools",
    }
    assert set(request["questions"]) == {
        "evidence_relevant",
        "evidence_gap_remains",
        "gap_closable_by_untried_tool",
        "answer_grounded",
        "answer_fits_role",
        "answer_complete",
        "worth_follow_up",
    }


def test_confidence_questions_reference_role_state_not_pasted_role():
    question = PipelinePrompts().confidence_request(_state())["questions"][
        "answer_fits_role"
    ]
    assert "`role.objective`" in question.instructions["compare"]
    assert "engineer" not in str(question.instructions)
