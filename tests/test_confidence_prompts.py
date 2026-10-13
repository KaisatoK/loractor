"""Offline tests for the scope/confidence classifier prompt payloads.

Two guarantees:

* the tool catalog shown to the classifiers stays in sync with the evidence
  tools the pipeline can actually call (``backend.pipeline.tools.__all__``);
* the confidence request carries the attempt history and revision count the
  confidence classifier uses.

No network access: the builders only render dicts from ``PipelineState``.
"""

import os

os.environ.setdefault("TYPESAFE_API_KEY", "test-key")

from backend.pipeline import tools
from backend.pipeline.prompts.builders import TOOL_CATALOG, PipelinePrompts


def _state():
    """A representative mid-revision state (one round already attempted)."""
    return {
        "normalized": "Why did checkout errors spike this morning?",
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
