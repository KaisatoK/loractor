"""Offline tests for the structured answer ledger, scope appendix and priority score."""

import os

os.environ.setdefault("TYPESAFE_API_KEY", "test-key")

from backend.pipeline import graph
from backend.pipeline.answers import (
    APPENDIX_HEADING,
    render_appendix,
    serialize_answer,
    strip_appendix,
)
from backend.pipeline.contracts import AnswerOutput
from backend.pipeline.priority import DERIVED_TOOL_NAME, compute_priorities, with_priority
from ui.rendering import canonicalize


def _rec(rid, kind, rows, tool="count_by_category_tool", **extra):
    return {
        "id": rid,
        "tool_name": tool,
        "arguments": {},
        "row_count": len(rows),
        "rows": rows,
        "kind": kind,
        "dataset": "OpenStack",
        "query": f"SELECT /* {rid} */ 1",
        "filters": {"start_time": "2017-05-16 00:00:00", "end_time": "2017-05-16 00:15:00"},
        "truncated": False,
        **extra,
    }


SEARCH = _rec(
    "R1", "search",
    [{"line_id": 42, "content": "Instance failed to spawn", "service": "nova.compute.manager"}],
    tool="search_logs_tool", truncated=True,
)
COUNTS = _rec("R2", "aggregate", [
    {"service": "nova.compute.manager", "category": "ERROR", "count": 30},
    {"service": "nova.compute.manager", "category": "WARNING", "count": 10},
    {"service": "nova.virt.libvirt.imagecache", "category": "WARNING", "count": 60},
    {"service": "nova.osapi_compute.wsgi.server", "category": "INFO", "count": 800},
])


def _answer(**fields):
    return AnswerOutput.model_validate({"blocks": [{"type": "text", "text": "Story."}], **fields})


# --- ledger ----------------------------------------------------------------


def test_findings_are_grouped_and_citations_tagged_by_kind():
    answer = _answer(findings=[
        {"id": "F1", "kind": "measured", "statement": "compute manager has 30 ERROR rows.",
         "result_ids": ["R2"]},
        {"id": "F2", "kind": "hypothesis", "statement": "Spawn fails on disk pressure.",
         "result_ids": ["R1"]},
        {"id": "F3", "kind": "limitation", "statement": "Root cause is not in the logs."},
    ])
    text = serialize_answer(answer, {"R1", "R2"}, [SEARCH, COUNTS])
    assert "**Measured**" in text and "**Hypotheses**" in text and "**Limitations**" in text
    assert "R2 (count)" in text and "R1 (examples)" in text
    assert text.index("**Measured**") < text.index("**Hypotheses**")


def test_measured_finding_without_valid_id_is_downgraded():
    answer = _answer(findings=[
        {"id": "F1", "kind": "measured", "statement": "There were 500 failures.",
         "result_ids": ["R9"]},
    ])
    text = serialize_answer(answer, {"R1"}, [SEARCH])
    assert "**Measured**" not in text
    assert "There were 500 failures. (not backed by a cited result)" in text


def test_measured_finding_citing_only_examples_is_flagged():
    answer = _answer(findings=[
        {"id": "F1", "kind": "measured", "statement": "The spawn error occurs.",
         "result_ids": ["R1"]},
    ])
    text = serialize_answer(answer, {"R1"}, [SEARCH])
    assert "(row-level evidence, not a frequency)" in text


def test_recommendations_render_reason_owner_and_basis():
    answer = _answer(recommendations=[
        {"action": "Check the compute host disk", "why": "highest ERROR count",
         "finding_ids": ["F1"], "owner_team": "Compute Platform"},
    ])
    text = serialize_answer(answer, set(), [])
    assert "1. **Check the compute host disk** — highest ERROR count" in text
    assert "owner: Compute Platform" in text and "based on F1" in text


def test_legacy_answer_without_records_has_no_appendix():
    text = serialize_answer(_answer(), set())
    assert text == "Story."


# --- appendix --------------------------------------------------------------


def test_appendix_states_scope_queries_and_truncation():
    text = render_appendix([SEARCH, COUNTS])
    assert text.startswith(APPENDIX_HEADING)
    assert "Datasets: OpenStack" in text
    assert "2017-05-16 00:00:00 – 2017-05-16 00:15:00" in text
    assert "Examples only (do not measure frequency): R1" in text
    assert "Measured counts/trends: R2" in text
    assert "```sql\nSELECT /* R2 */ 1\n```" in text
    assert "truncated: yes" in text


def test_appendix_reports_unknown_query_and_truncation_for_legacy_records():
    legacy = {"id": "R1", "tool_name": "x", "arguments": {"a": 1}, "row_count": 1,
              "rows": [{"a": 1}], "kind": None, "query": None, "truncated": None}
    text = render_appendix([legacy])
    assert "query: not reported by this tool" in text
    assert "truncated: unknown" in text
    assert "Datasets: not reported" in text


def test_strip_appendix_keeps_narrative_and_ledger():
    answer = _answer(findings=[{"id": "F1", "kind": "limitation", "statement": "x"}])
    text = serialize_answer(answer, {"R1"}, [SEARCH])
    stripped = strip_appendix(text)
    assert APPENDIX_HEADING not in stripped and "SELECT" not in stripped
    assert "Story." in stripped and "**Limitations**" in stripped


def test_appendix_renders_as_prose_not_tables_in_ui():
    # The UI must not mistake anything in the appendix for a result marker.
    text = serialize_answer(_answer(blocks=[{"type": "table", "result_id": "R2"}]),
                            {"R1", "R2"}, [SEARCH, COUNTS])
    tables = [value for kind, value in canonicalize(text) if kind == "table"]
    assert tables == ["R2"]


# --- priority --------------------------------------------------------------


def test_priority_ranks_by_measured_counts_and_ignores_info():
    ranking = compute_priorities([COUNTS], team_for=lambda c: "T:" + c)
    assert ranking["dimension"] == "service"
    rows = ranking["rows"]
    names = [r["service"] for r in rows]
    assert "nova.osapi_compute.wsgi.server" not in names  # INFO only
    # compute manager: 40/100 share, 75% errors -> beats imagecache 60/100, 0% errors
    top = rows[0]
    assert top["service"] == "nova.compute.manager"
    assert top["non_info_rows"] == 40 and top["error_rows"] == 30
    assert top["team"] == "T:nova.compute.manager"
    assert ranking["terms_used"] == ["volume_share", "error_share"]
    assert ranking["source_ids"] == ["R2"]


def test_priority_uses_trend_from_period_comparison():
    compare = _rec("R3", "aggregate", [
        {"service": "nova.virt.libvirt.imagecache", "category": "WARNING",
         "baseline_count": 10, "recent_count": 50},
        {"service": "nova.compute.manager", "category": "ERROR",
         "baseline_count": 20, "recent_count": 10},
    ], tool="compare_periods_tool")
    ranking = compute_priorities([COUNTS, compare])
    assert "trend_up" in ranking["terms_used"]
    by_name = {r["service"]: r for r in ranking["rows"]}
    assert by_name["nova.virt.libvirt.imagecache"]["trend_up"] == 1.0
    assert by_name["nova.compute.manager"]["trend_up"] == 0.0
    assert ranking["source_ids"] == ["R2", "R3"]


def test_priority_needs_counts():
    assert compute_priorities([SEARCH]) is None


def test_with_priority_is_idempotent_and_appends_derived_record():
    once = with_priority([SEARCH, COUNTS])
    twice = with_priority(once)
    assert [r["id"] for r in once] == ["R1", "R2", "R3"]
    assert [r["id"] for r in twice] == ["R1", "R2", "R3"]
    derived = once[-1]
    assert derived["tool_name"] == DERIVED_TOOL_NAME and derived["kind"] == "derived"
    assert "score =" in derived["formula"]
    assert "computed in code from R2" in render_appendix(once)
    assert "terms_used" not in render_appendix(once)  # derived adds no scope


# --- answer node -----------------------------------------------------------


def _answer_state(role):
    return {
        "role": {"name": role, "objective": "o"},
        "effort": "Medium",
        "normalized": {"normalized_question": "q"},
        "evidence_report": {},
        "tool_results": [SEARCH, COUNTS],
    }


def test_answer_node_adds_priority_for_manager_not_engineer(monkeypatch):
    prompts_seen = []

    def fake_generate(prompt):
        prompts_seen.append(prompt)
        return _answer()

    monkeypatch.setattr(graph, "generate_answer", fake_generate)
    manager = graph.answer_node(_answer_state("manager"))
    engineer = graph.answer_node(_answer_state("engineer"))
    assert [r["id"] for r in manager["tool_results"]] == ["R1", "R2", "R3"]
    assert [r["id"] for r in engineer["tool_results"]] == ["R1", "R2"]
    assert "priority_score" in prompts_seen[0] and "priority_score" not in prompts_seen[1]
    assert APPENDIX_HEADING in manager["answer"]
    # Role-specific answer rules (from the role skill) reach the prompt.
    assert "At most three priorities" in prompts_seen[0]
    assert "before/after timeline" in prompts_seen[1]
