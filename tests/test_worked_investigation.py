"""Offline tests for the worked-investigation document generator.

``build_markdown`` is pure, so these use synthetic role runs; the live runner is
exercised separately by ``bash ./worked_investigation.sh``.
"""

import os

os.environ.setdefault("TYPESAFE_API_KEY", "test-key")

from backend import worked_investigation as wi
from backend.pipeline.answers import serialize_answer
from backend.pipeline.contracts import AnswerOutput


def _rec(rid, kind, rows, tool, query="SELECT 1", truncated=False, **extra):
    return {
        "id": rid, "tool_name": tool, "arguments": {"x": 1}, "row_count": len(rows),
        "rows": rows, "kind": kind, "dataset": "OpenStack", "query": query,
        "filters": {"category": "ERROR"}, "truncated": truncated, **extra,
    }


SEARCH = _rec("R1", "search", [{"line_id": 7, "content": "boom | pipe"}], "search_logs_tool", truncated=True)
COUNT = _rec("R2", "aggregate", [{"service": "nova.compute.manager", "count": 9}], "count_by_category_tool")


def _answer(records, **fields):
    answer = AnswerOutput.model_validate(
        {"blocks": [{"type": "text", "text": "Story."}, {"type": "table", "result_id": "R2"}], **fields}
    )
    return serialize_answer(answer, {r["id"] for r in records}, records)


def _run(role, records, **fields):
    return {
        "role": role, "question": f"{role} question?", "effort": "High", "scope": "find_evidence",
        "tool_results": records, "answer": _answer(records, **fields),
        "evidence_report": {"summary": "found", "evidence": [], "hypotheses": [
            {"hypothesis": "disk", "status": "inconclusive", "contradicting_evidence_ids": ["E2"],
             "next_check": "inspect disk"}]},
    }


FINDINGS = {
    "findings": [
        {"id": "F1", "kind": "measured", "statement": "9 ERROR rows.", "result_ids": ["R2"]},
        {"id": "F2", "kind": "hypothesis", "statement": "Disk pressure.", "result_ids": ["R1"]},
        {"id": "F3", "kind": "limitation", "statement": "No impact data."},
    ],
    "recommendations": [{"action": "Check disk", "why": "top count", "owner_team": "Compute Platform"}],
}


def test_markdown_has_every_role_and_required_sections():
    runs = [_run(role, [SEARCH, COUNT], **FINDINGS) for role in wi.ROLES]
    text = wi.build_markdown(runs, topic="spawn failures", generated="now")
    for title in ("## Engineer", "## Team lead", "## Manager"):
        assert title in text
    for heading in ("### Tool calls", "### Executed queries", "### Relevant results",
                    "### Evidence report", "### Recommended action",
                    "## What the data does and does not establish", "## Data scope"):
        assert heading in text
    assert "```sql\nSELECT 1\n```" in text
    assert "next check: inspect disk" in text and "contradicted by E2" in text
    assert "1. **Check disk**" in text
    assert "Datasets queried: OpenStack" in text


def test_markers_become_tables_and_pipes_are_escaped():
    run = _run("engineer", [SEARCH, COUNT])
    section = wi._role_section(run)
    assert "[[RESULT:" not in section
    assert "| service | count |" in section and "nova.compute.manager" in section
    assert "boom \\| pipe" in section  # pipe escaped inside table cells


def test_appendix_is_not_duplicated_in_answer_section():
    run = _run("engineer", [SEARCH, COUNT])
    section = wi._role_section(run)
    assert wi.APPENDIX_HEADING not in section  # queries have their own section


def test_establishes_section_splits_measured_from_open_items():
    runs = [_run("manager", [SEARCH, COUNT], **FINDINGS)]
    text = wi._establishes(runs)
    established, not_established = text.split("**Not established")
    assert "(Manager) **F1** 9 ERROR rows." in established
    assert "**F2** Disk pressure." in not_established and "**F3** No impact data." in not_established
    assert "not incidents" in text


def test_coverage_flags_missing_search_for_engineer():
    run = _run("engineer", [COUNT])
    assert wi.coverage([COUNT]) == {"search": False, "aggregation": True}
    assert "No full-text search result" in wi._role_section(run)
    team = _run("team_lead", [SEARCH])
    assert "No whole-scope aggregation" in wi._role_section(team)


def test_non_investigation_route_and_errors_are_called_out():
    clarify = {"role": "engineer", "question": "q", "effort": "Low", "scope": "clarify",
               "tool_results": [], "answer": "Which service?"}
    failed = {"role": "manager", "question": "q", "effort": "Low", "error": "RuntimeError: boom"}
    text = wi.build_markdown([clarify, failed], topic="t", generated="now")
    assert "Route was `clarify`" in text and "Run failed: `RuntimeError: boom`" in text


def test_question_templates_cover_all_roles():
    for role in wi.ROLES:
        assert "{topic}" in wi.QUESTION_TEMPLATES[role]
    args = wi.parse_args(["--topic", "x", "--manager-question", "custom?"])
    assert args.manager_question == "custom?" and args.engineer_question is None
