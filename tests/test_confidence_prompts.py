"""Offline tests for the scope/confidence classifier prompt payloads.

Three guarantees:

* the tool catalog shown to the classifiers stays in sync with the evidence
  tools the pipeline can actually call (``backend.pipeline.tools.__all__``);
* the scope request carries the shared pipeline mission, schema and tool
  catalog, plus the RAW conversation (not the normalized rewrite) the scope
  classifier uses to judge whether a queryable target is named;
* the confidence request carries the attempt history and revision count the
  confidence classifier uses.

No network access: the builders only render dicts from ``PipelineState``.
"""

import os

os.environ.setdefault("TYPESAFE_API_KEY", "test-key")

from backend.db.config import LOG_SCHEMA
from backend.pipeline import tools
from backend.pipeline.prompts.builders import (
    PIPELINE_MISSION,
    TOOL_CATALOG,
    PipelinePrompts,
)


def _state():
    """A representative mid-revision state (one round already attempted)."""
    return {
        "conversation": "user: Why did checkout errors spike this morning?",
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
        "revision_feedback": "More evidence is required.",
        "evidence_report_history": [{"round": 1}],
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
        "revision_count": 1,
    }


def test_tool_catalog_matches_available_tools():
    assert set(TOOL_CATALOG) == set(tools.__all__)


def test_scope_request_includes_tool_catalog():
    request = PipelinePrompts().scope_request(_state())
    assert request["state"]["tool_catalog"] == TOOL_CATALOG


def test_scope_request_includes_mission():
    request = PipelinePrompts().scope_request(_state())
    assert request["state"]["mission"] == PIPELINE_MISSION


def test_scope_request_carries_latest_message_and_answer():
    request = PipelinePrompts().scope_request(_state())
    assert request["state"]["latest_message"] == "Why did checkout errors spike this morning?"
    assert request["state"]["latest_answer"] == "Checkout errors rose 3x after 09:00."


def test_scope_request_sends_raw_transcript_not_normalized():
    # The classifier must judge the raw conversation: the normalizer's rewrite
    # drops the wording that names the queryable targets.
    request = PipelinePrompts().scope_request(_state())
    assert request["state"]["transcript"] == _state()["conversation"]
    assert "normalized" not in request["state"]


def test_scope_request_asks_has_queryable_scope():
    request = PipelinePrompts().scope_request(_state())
    assert "has_queryable_scope" in request["questions"]
    assert "needs_clarification" not in request["questions"]


def test_scope_request_keeps_shared_schema_and_catalog():
    payload = PipelinePrompts().scope_request(_state())["state"]
    assert payload["database_schema"] == LOG_SCHEMA
    assert payload["tool_catalog"] == TOOL_CATALOG


def test_scope_request_asks_proceed_without_clarification():
    request = PipelinePrompts().scope_request(_state())
    assert "proceed_without_clarification" in request["questions"]


def test_scope_request_does_not_carry_clarify_count():
    # Authorization must stay independent of the retry count.
    request = PipelinePrompts().scope_request(_state())
    assert "clarify_count" not in request["state"]


def test_confidence_request_includes_catalog_history_and_budgets():
    request = PipelinePrompts().confidence_request(_state())
    payload = request["state"]

    assert payload["tool_catalog"] == TOOL_CATALOG
    assert payload["evidence_report_history"] == [{"round": 1}]
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
    # Medium: max_tool_calls=5, max_iterations=1.
    assert payload["revision_count"] == 1


def test_confidence_request_keeps_existing_keys():
    payload = PipelinePrompts().confidence_request(_state())["state"]
    assert set(payload) >= {
        "request",
        "role",
        "evidence_report",
        "answer",
        "revision_feedback",
    }
