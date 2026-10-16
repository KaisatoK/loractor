"""Gated live checks for the confidence classifier's code-owned policy.

Offline, only case-data validation runs. Run this module alone with
``bash ./confidence.sh`` so the real TypeSafe key is loaded before imports.
"""

from __future__ import annotations

import html
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

RUN_LIVE = os.environ.get("RUN_LIVE_CONFIDENCE") == "1"
if RUN_LIVE:
    from dotenv import load_dotenv

    load_dotenv(override=True)

import pytest

from backend.pipeline.decision import decide_confidence
from backend.pipeline.prompts import PipelinePrompts
from backend.pipeline.decision import classifier

prompts = PipelinePrompts()


@dataclass(frozen=True)
class ConfidenceCase:
    id: str
    request: str
    report: dict
    answer: str
    expected_state: str
    expected_reason: str
    attempted_tools: list[dict]
    objective: str = "Find the root cause of a specific error."


_BASE_REPORT = {
    "summary": (
        "Between 09:00 and 10:00, checkout timeout errors increased from 2 to 7. "
        "Connection resets in checkout-service preceded the increase; the report "
        "does not support a database connection-pool cause."
    ),
    "evidence": [
        {
            "id": "E1",
            "name": "Checkout timeout count",
            "description": "Counts timeout records in the requested window.",
            "tool_used": [
                {
                    "name": "count_by_category_tool",
                    "parameters": {"category": "timeout"},
                    "status": "success",
                    "error_message": None,
                }
            ],
            "availability": "available",
            "result_ids": ["R1"],
        }
    ],
    "hypotheses": [],
}

CASES = [
    ConfidenceCase(
        id="grounded-complete-answer",
        request="Why did checkout timeout errors increase?",
        report=_BASE_REPORT,
        answer="Connection resets in checkout-service preceded the increase from 2 to 7 timeout errors between 09:00 and 10:00, based on E1.",
        expected_state="end",
        expected_reason="none",
        attempted_tools=[{"tool_name": "search_logs_tool", "arguments": {"query_text": "timeout"}}],
    ),
    ConfidenceCase(
        id="unsupported-claim",
        request="Why did checkout timeout errors increase?",
        report=_BASE_REPORT,
        answer="The database connection pool caused the increase, but the report only supports connection resets in checkout-service.",
        expected_state="answer",
        expected_reason="unsupported_answer",
        attempted_tools=[{"tool_name": "search_logs_tool", "arguments": {"query_text": "timeout"}}],
    ),
    ConfidenceCase(
        id="wrong-role",
        request="Why did checkout timeout errors increase?",
        report=_BASE_REPORT,
        answer="Connection resets in checkout-service preceded the increase. Engineers should inspect the connection pool and trace the timeout stack.",
        expected_state="answer",
        expected_reason="role_mismatch",
        attempted_tools=[{"tool_name": "search_logs_tool", "arguments": {"query_text": "timeout"}}],
        objective="Prioritize issues by business impact and recommend what management should address first.",
    ),
    ConfidenceCase(
        id="incomplete-answer",
        request="What was the checkout timeout count between 09:00 and 10:00?",
        report=_BASE_REPORT,
        answer="Checkout timeout errors increased.",
        expected_state="answer",
        expected_reason="incomplete_answer",
        attempted_tools=[{"tool_name": "search_logs_tool", "arguments": {"query_text": "timeout"}}],
    ),
    ConfidenceCase(
        id="closable-evidence-gap",
        request="Why did checkout timeout errors increase, and which service caused it?",
        report={
            "summary": "Timeouts increased, but no service breakdown was collected.",
            "evidence": [],
            "hypotheses": [],
        },
        answer="Timeouts increased, but the affected service is unknown.",
        expected_state="find_evidence",
        expected_reason="missing_evidence",
        attempted_tools=[{"tool_name": "search_logs_tool", "arguments": {"query_text": "timeout"}}],
    ),
    ConfidenceCase(
        id="already-attempted-gap",
        request="What was the customer revenue impact of checkout timeout errors?",
        report={
            "summary": "The log data cannot measure customer revenue impact or external business outcomes.",
            "evidence": [],
            "hypotheses": [],
        },
        answer="The available log data does not establish customer revenue impact or external business outcomes.",
        expected_state="end",
        expected_reason="none",
        attempted_tools=[
            {"tool_name": "search_logs_tool", "arguments": {"query_text": "timeout"}},
            {"tool_name": "top_in_category_by_service_tool", "arguments": {"category": "timeout"}},
            {"tool_name": "count_by_category_tool", "arguments": {"type": "service"}},
            {"tool_name": "get_data_tool", "arguments": {"type": "service"}},
            {"tool_name": "get_trend_tool", "arguments": {"type": "service"}},
            {"tool_name": "compare_periods_tool", "arguments": {}},
            {"tool_name": "get_log_by_line_id_tool", "arguments": {"line_id": 1}},
        ],
        objective="Prioritize issues by business impact and state when the log data cannot measure impact.",
    ),
]

_REPORT: list[dict] = []
_ANNOUNCED = False


def _report_path() -> Path:
    return Path(os.environ.get("RUN_LIVE_CONFIDENCE_REPORT", ".data/confidence_report.html"))


def _write_report() -> None:
    global _ANNOUNCED
    if not RUN_LIVE:
        return
    path = _report_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for row in _REPORT:
        values = [
            row["id"],
            row.get("state", "not run"),
            row.get("reason", "not run"),
            row.get("expected_state", ""),
            row.get("expected_reason", ""),
            "✓" if row.get("ok") else "✗",
            json.dumps(row.get("signals", {}), sort_keys=True),
            row.get("error", ""),
        ]
        rows.append("<tr>" + "".join(f"<td>{html.escape(str(value))}</td>" for value in values) + "</tr>")
    body = "<html><head><meta charset='utf-8'><title>Confidence report</title></head><body>"
    body += f"<h1>Confidence report</h1><p>Generated {datetime.now(timezone.utc).isoformat()}</p>"
    body += "<table border='1'><tr><th>case</th><th>state</th><th>reason</th><th>expected state</th><th>expected reason</th><th>ok</th><th>signals</th><th>error</th></tr>"
    body += "".join(rows) + "</table></body></html>"
    path.write_text(body, encoding="utf-8")
    if not _ANNOUNCED:
        _ANNOUNCED = True
        print(f"HTML report: {path.resolve().as_uri()}")


def _state(case: ConfidenceCase) -> dict:
    return {
        "normalized": case.request,
        "role": {"name": "engineer", "objective": case.objective},
        "evidence_report": case.report,
        "answer": case.answer,
        "tool_catalog": {},
        "attempted_tools": case.attempted_tools,
    }


@pytest.mark.skipif(not RUN_LIVE, reason="live confidence test — run with bash ./confidence.sh")
@pytest.mark.parametrize("case", CASES, ids=lambda case: case.id)
def test_live_confidence(case: ConfidenceCase):
    response = classifier.invoke(prompts.confidence_request(_state(case)))
    result = decide_confidence(response, "High", revision_count=0)
    signals = {name: answer.noul for name, answer in response.nouls.items()}
    row = {
        "id": case.id,
        "state": result["next_state"],
        "reason": result["revision_reason"],
        "expected_state": case.expected_state,
        "expected_reason": case.expected_reason,
        "signals": signals,
        "ok": result["next_state"] == case.expected_state and result["revision_reason"] == case.expected_reason,
    }
    _REPORT.append(row)
    _write_report()
    assert row["ok"], f"{case.id}: expected {case.expected_state}/{case.expected_reason}, got {result}; signals={signals}"


def test_case_data_is_valid():
    ids = [case.id for case in CASES]
    assert len(ids) == len(set(ids))
    assert all(case.request and case.answer for case in CASES)
    assert all(case.expected_state in {"find_evidence", "answer", "end"} for case in CASES)
