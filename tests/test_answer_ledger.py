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
from backend.pipeline.priority import (
    DERIVED_TOOL_NAME,
    MAX_ROWS,
    compute_priorities,
    with_priority,
)
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


def _search_record(returned: int, total: int | None, truncated: bool | None) -> dict:
    return {
        "id": "R1", "tool_name": "search_logs_tool", "arguments": {}, "kind": "search",
        "row_count": returned, "returned": returned, "rows": [{"n": 1}] * returned,
        "dataset": "openstack", "query": "SELECT 1", "filters": {},
        "total_matches": total, "truncated": truncated,
    }


def test_a_complete_result_still_shows_its_total():
    """3 of 3: nothing was cut, but the count is stated, so 'all of them' is visible."""
    text = render_appendix([_search_record(3, 3, False)])
    assert "search, 3 rows; truncated: no (3 of 3 matching)" in text


def test_a_cut_result_shows_shown_of_total():
    text = render_appendix([_search_record(10, 24, True)])
    assert "truncated: yes (10 of 24 rows returned; more matching rows exist)" in text


def test_a_result_without_a_total_states_nothing_extra():
    text = render_appendix([_search_record(3, None, False)])
    assert "search, 3 rows; truncated: no\n" in text


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


def test_priority_prefers_the_dimension_it_can_trend():
    """A manager run brings template counts *and* a service period comparison.

    The ranking must pick the dimension whose trend it can measure (service), not
    the count-only one (template), so the derived record is trend-aware instead of
    leaving the answer to rank the counts itself.
    """
    templates = _rec("R5", "aggregate", [
        {"template": "data TLB error interrupt", "severity": "FATAL", "count": 152_734},
    ], tool="count_by_category_tool")
    compare = _rec("R3", "aggregate", [
        {"service": "KERNEL", "severity": "FATAL",
         "baseline_count": 593_592, "recent_count": 33_164},
        {"service": "APP", "severity": "FATAL",
         "baseline_count": 89_716, "recent_count": 138_820},
    ], tool="compare_periods_tool")
    ranking = compute_priorities([templates, compare])
    assert ranking["dimension"] == "service"
    assert "trend_up" in ranking["terms_used"]
    by_name = {r["service"]: r for r in ranking["rows"]}
    # The comparison's two windows are added: KERNEL 593,592 + 33,164.
    assert by_name["KERNEL"]["non_info_rows"] == 626_756
    assert by_name["KERNEL"]["error_rows"] == 626_756
    assert by_name["KERNEL"]["trend_up"] == 0.0
    assert by_name["APP"]["non_info_rows"] == 228_536
    assert by_name["APP"]["trend_up"] == 0.55
    # Only the dimension that was ranked is cited as a source.
    assert ranking["source_ids"] == ["R3"]


def test_priority_without_a_comparison_keeps_the_table_order_dimension():
    # No comparison to trend, so the first count-bearing dimension still wins.
    templates = _rec("R5", "aggregate", [
        {"template": "t", "severity": "FATAL", "count": 10},
    ])
    service = _rec("R2", "aggregate", [
        {"service": "s", "severity": "ERROR", "count": 5},
    ])
    assert compute_priorities([templates, service])["dimension"] == "template"


def _window_record(rid, rows, start, end, *, truncated=False):
    return _rec(
        rid, "aggregate", rows, tool="count_by_category_tool", truncated=truncated,
        filters={"start_time": start, "end_time": end, "group_by": "template"},
    )


def test_priority_pairs_two_windowed_counts_into_a_trend():
    """Two halves brought as separate counts give every candidate both windows.

    A period comparison is ordered by volume_change and truncated, so a large,
    flat template gets no trend; pairing the halves measures the trend for it.
    """
    baseline = _window_record(
        "R5",
        [{"template": "rises", "severity": "ERROR", "count": 10},
         {"template": "flat", "severity": "ERROR", "count": 1000}],
        "2017-05-14 19:39:01", "2017-05-16 04:00:00",
    )
    recent = _window_record(
        "R6",
        [{"template": "rises", "severity": "ERROR", "count": 40},
         {"template": "flat", "severity": "ERROR", "count": 1000}],
        "2017-05-16 04:00:00", "2017-05-17 12:02:36",
    )
    ranking = compute_priorities([baseline, recent])
    assert ranking["dimension"] == "template"
    assert "trend_up" in ranking["terms_used"]
    by_name = {r["template"]: r for r in ranking["rows"]}
    assert by_name["rises"]["non_info_rows"] == 50      # 10 + 40, not max
    assert by_name["rises"]["trend_up"] == 1.0
    assert by_name["flat"]["non_info_rows"] == 2000
    assert by_name["flat"]["trend_up"] == 0.0
    assert ranking["source_ids"] == ["R5", "R6"]        # both halves cited


def test_priority_does_not_pair_the_same_window_counted_twice():
    rows = [{"template": "a", "severity": "ERROR", "count": 10}]
    same = _window_record("R1", rows, "2017-05-14 00:00:00", "2017-05-17 00:00:00")
    again = _window_record("R2", rows, "2017-05-14 00:00:00", "2017-05-17 00:00:00")
    ranking = compute_priorities([same, again])
    # One window, not two halves: overlapping counts are maxed, never summed.
    assert ranking["rows"][0]["non_info_rows"] == 10
    assert "trend_up" not in ranking["terms_used"]


def test_priority_does_not_pair_overlapping_windows():
    baseline = _window_record(
        "R1", [{"template": "a", "severity": "ERROR", "count": 10}],
        "2017-05-14 00:00:00", "2017-05-16 12:00:00",
    )
    recent = _window_record(
        "R2", [{"template": "a", "severity": "ERROR", "count": 5}],
        "2017-05-16 00:00:00", "2017-05-17 00:00:00",
    )
    ranking = compute_priorities([baseline, recent])
    assert ranking["rows"][0]["non_info_rows"] == 10
    assert "trend_up" not in ranking["terms_used"]


def test_priority_drops_a_value_missing_from_a_truncated_half():
    baseline = _window_record(
        "R1", [{"template": "only_baseline", "severity": "ERROR", "count": 10}],
        "2017-05-14 00:00:00", "2017-05-16 00:00:00",
    )
    recent = _window_record(
        "R2", [{"template": "other", "severity": "ERROR", "count": 5}],
        "2017-05-16 00:00:00", "2017-05-17 00:00:00", truncated=True,
    )
    ranking = compute_priorities([baseline, recent])
    names = {row["template"] for row in ranking["rows"]}
    assert "other" in names and "only_baseline" not in names


def test_priority_does_not_synthesize_a_pair_when_a_comparison_exists():
    compare = _rec("R3", "aggregate", [
        {"template": "a", "severity": "ERROR", "baseline_count": 10, "recent_count": 40},
    ], tool="compare_periods_tool")
    baseline = _window_record(
        "R4", [{"template": "a", "severity": "ERROR", "count": 10}],
        "2017-05-14 00:00:00", "2017-05-16 00:00:00",
    )
    recent = _window_record(
        "R5", [{"template": "a", "severity": "ERROR", "count": 40}],
        "2017-05-16 00:00:00", "2017-05-17 00:00:00",
    )
    ranking = compute_priorities([compare, baseline, recent])
    assert ranking["rows"][0]["non_info_rows"] == 50    # not doubled by a pair
    assert ranking["rows"][0]["trend_up"] == 1.0


def test_priority_ranks_each_dataset_on_its_own():
    """Shares are within a dataset, so a large corpus cannot drown a small one."""
    bgl = _rec("R1", "aggregate", [
        {"service": "KERNEL", "severity": "FATAL", "count": 900},
        {"service": "MMCS", "severity": "ERROR", "count": 100},
    ], dataset="bgl")
    otel = _rec("R2", "aggregate", [
        {"service": "frontend", "severity": "error", "count": 1_000_000},
    ], dataset="otel_s3")
    ranking = compute_priorities([bgl, otel])
    by = {(r["dataset"], r["service"]): r for r in ranking["rows"]}
    assert by[("bgl", "KERNEL")]["volume_share_pct"] == 90.0
    assert by[("otel_s3", "frontend")]["volume_share_pct"] == 100.0
    # Ranks restart per dataset, and the rows are ordered by dataset then rank.
    assert [(r["dataset"], r["rank"]) for r in ranking["rows"]] == [
        ("bgl", 1), ("bgl", 2), ("otel_s3", 1),
    ]
    assert ranking["total_ranked"] == 3


def test_priority_marks_approximate_datasets():
    exact = _rec("R1", "aggregate", [
        {"service": "a", "severity": "ERROR", "count": 10},
    ], dataset="bgl")
    approximate = _rec("R2", "aggregate", [
        {"service": "b", "severity": "error", "count": 20},
    ], dataset="otel_s3", query_stats={"approximate": True})
    assert compute_priorities([exact, approximate])["approximate_datasets"] == ["otel_s3"]
    assert compute_priorities([exact])["approximate_datasets"] == []


def test_priority_splits_a_multi_dataset_record_by_row_dataset():
    team = _rec("R1", "aggregate", [
        {"dataset": "bgl", "team": "Kernel", "severity": "ERROR", "count": 100},
        {"dataset": "openstack", "team": "Kernel", "severity": "ERROR", "count": 1},
    ], tool="count_by_team_tool", dataset=None)
    ranking = compute_priorities([team])
    assert ranking["dimension"] == "team"
    by = {(r["dataset"], r["team"]): r["volume_share_pct"] for r in ranking["rows"]}
    assert by == {("bgl", "Kernel"): 100.0, ("openstack", "Kernel"): 100.0}


def test_priority_max_rows_can_exceed_the_default():
    """The pipeline's derived record keeps the default cap; a caller with a larger
    allowance (``rank_priorities_tool``) can ask for more."""
    rows = _rec("R1", "aggregate", [
        {"service": f"svc-{i}", "category": "ERROR", "count": 100 - i}
        for i in range(30)
    ])
    assert len(compute_priorities([rows], max_rows=25)["rows"]) == 25
    assert len(compute_priorities([rows])["rows"]) == MAX_ROWS


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


def test_answer_node_flags_a_narrative_total_no_result_shows(monkeypatch):
    answer = _answer(blocks=[{
        "type": "text",
        "text": "There were 988,783 problem log entries across the datasets.",
    }])
    monkeypatch.setattr(graph, "generate_answer", lambda prompt: answer)
    out = graph.answer_node(_answer_state("manager"))
    # 988,783 is a sum nobody ran; it reads as measured, so code notes it.
    assert "988,783" in out["answer"]
    assert "no cited result shows" in out["answer"]


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


def test_priority_skips_zero_count_values_instead_of_dividing_by_zero():
    counts = _rec("R1", "aggregate", [
        {"service": "a", "category": "ERROR", "count": 0},
        {"service": "b", "category": "ERROR", "count": 5},
    ])
    ranking = compute_priorities([counts])
    assert [r["service"] for r in ranking["rows"]] == ["b"]


def test_priority_trend_keeps_the_newest_bucket_on_odd_length():
    counts = _rec("R1", "aggregate", [
        {"service": "a", "category": "WARNING", "count": 6},
        {"service": "b", "category": "WARNING", "count": 6},
    ])
    # Odd length: the oldest bucket is dropped. a: 0 | 1 vs 1 (flat);
    # b: 9 | 1 vs 5 (the newest bucket 5 must count, so b is rising).
    trend_a = _rec("R2", "trend", [{"count": n} for n in (0, 1, 1)],
                   tool="get_trend_tool", filters={"service": "a"})
    trend_b = _rec("R3", "trend", [{"count": n} for n in (9, 1, 5)],
                   tool="get_trend_tool", filters={"service": "b"})
    ranking = compute_priorities([counts, trend_a, trend_b])
    by_name = {r["service"]: r for r in ranking["rows"]}
    assert by_name["a"]["trend_up"] == 0.0
    assert by_name["b"]["trend_up"] == 1.0


def test_priority_trend_sums_a_per_severity_series_per_bucket_without_info():
    counts = _rec("R1", "aggregate", [
        {"service": "a", "category": "WARNING", "count": 6},
    ])
    # group_by="severity": two rows per bucket. INFO is ignored, so the
    # problem series is 1, 1 | 2, 4 -> baseline 2, recent 6 -> rising.
    rows = [
        {"bucket": f"2017-05-1{d} 00:00:00", "severity": sev, "count": n}
        for d, (warn, info) in enumerate([(1, 100), (1, 0), (2, 100), (4, 0)])
        for sev, n in (("WARNING", warn), ("INFO", info))
    ]
    trend = _rec("R2", "trend", rows, tool="get_trend_tool", filters={"service": "a"})
    ranking = compute_priorities([counts, trend])
    assert ranking["rows"][0]["trend_up"] == 1.0
    assert "R2" in ranking["source_ids"]


def test_priority_ignores_a_trend_whose_rows_were_cut():
    counts = _rec("R1", "aggregate", [
        {"service": "a", "category": "WARNING", "count": 6},
    ])
    trend = _rec("R2", "trend", [{"count": n} for n in (0, 9)],
                 tool="get_trend_tool", filters={"service": "a"}, rows_capped=True)
    ranking = compute_priorities([counts, trend])
    assert ranking["rows"][0]["trend_up"] == "n/a"
    assert "R2" not in ranking["source_ids"]


def test_priority_reads_the_registry_severity_column():
    # The dataset registry returns canonical ``severity`` (not ``category``) and
    # ``entity_id`` (not ``pid``); INFO rows must still be ignored.
    counts = _rec("R1", "aggregate", [
        {"service": "a", "severity": "INFO", "count": 90},
        {"service": "a", "severity": "ERROR", "count": 10},
        {"service": "b", "severity": "WARNING", "count": 30},
    ])
    ranking = compute_priorities([counts])
    by_name = {r["service"]: r for r in ranking["rows"]}
    assert by_name["a"]["non_info_rows"] == 10 and by_name["a"]["error_rows"] == 10
    assert by_name["b"]["non_info_rows"] == 30 and by_name["b"]["error_rows"] == 0
    assert "error_share" in ranking["terms_used"]


def test_priority_ranks_by_entity_id_when_that_is_the_grouping():
    counts = _rec("R1", "aggregate", [
        {"entity_id": "2931", "severity": "WARNING", "count": 5},
    ])
    assert compute_priorities([counts])["dimension"] == "entity_id"


def test_priority_levels_cover_every_registered_dataset():
    # Every severity a registered corpus declares is either informational or a
    # problem level the score knows how to weigh; an unknown informational value
    # would otherwise be ranked as a problem.
    from backend.db.datasets import all_datasets
    from backend.pipeline.priority import INFO_LEVELS, SEVERE_LEVELS

    known = INFO_LEVELS | SEVERE_LEVELS | {"WARNING", "WARN"}
    for dataset in all_datasets():
        unknown = {v.upper() for v in dataset.severity_values} - known
        assert not unknown, (dataset.id, unknown)
