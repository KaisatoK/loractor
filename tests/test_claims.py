"""Offline tests for the code-side number check on measured findings."""

import os

os.environ.setdefault("TYPESAFE_API_KEY", "test-key")

from backend.pipeline.answers import serialize_answer
from backend.pipeline.claims import unsupported_numbers
from backend.pipeline.contracts import AnswerOutput


def _rec(rid, rows, kind="aggregate", tool="count_by_category_tool", **extra):
    return {
        "id": rid, "tool_name": tool, "arguments": {}, "row_count": len(rows),
        "rows": rows, "kind": kind, "dataset": "openstack", "query": "SELECT 1",
        "filters": {}, "truncated": False, **extra,
    }


COUNTS = _rec("R1", [
    {"service": "nova.compute.manager", "severity": "ERROR", "count": 30},
    {"service": "nova.compute.manager", "severity": "WARNING", "count": 1969},
    {"service": "nova.virt.libvirt.imagecache", "severity": "WARNING", "count": 60,
     "share_pct": 96.8, "ratio": 0.25},
])


# --- the check ---------------------------------------------------------------


def test_a_statement_without_numbers_passes():
    assert unsupported_numbers("The compute manager logs errors.", [COUNTS]) == []


def test_numbers_present_in_a_cited_row_pass():
    assert unsupported_numbers("30 ERROR rows from the compute manager.", [COUNTS]) == []


def test_a_number_no_row_shows_is_reported():
    assert unsupported_numbers("There were 500 failures.", [COUNTS]) == ["500"]


def test_a_sum_the_model_made_up_is_not_measured():
    # 30 + 1969 = 1999 is arithmetic nobody ran.
    assert unsupported_numbers("1999 problem rows in total.", [COUNTS]) == ["1999"]


def test_thousands_separators_are_normalised():
    assert unsupported_numbers("1,969 warnings.", [COUNTS]) == []
    assert unsupported_numbers("1969 warnings.", [COUNTS]) == []


def test_trailing_zeros_and_leading_zeros_do_not_matter():
    assert unsupported_numbers("30.0 errors.", [COUNTS]) == []
    assert unsupported_numbers("060 rows.", [COUNTS]) == []


def test_each_missing_figure_is_reported_once_in_statement_order():
    assert unsupported_numbers("7 then 9 then 7 again.", [COUNTS]) == ["7", "9"]


def test_ids_with_digits_are_labels_not_measurements():
    text = "R1 and F2 mention node-5 and req-0a1b2c3d."
    assert unsupported_numbers(text, [COUNTS]) == []


def test_the_sentence_ending_period_is_not_part_of_the_number():
    assert unsupported_numbers("There were 30.", [COUNTS]) == []


def test_numbers_inside_text_cells_count():
    hit = _rec("R2", [{"record_id": 56, "message": "pid 2931 spawn failed"}],
               kind="search", tool="search_logs_tool")
    assert unsupported_numbers("Line 56 from pid 2931 failed.", [hit]) == []
    assert unsupported_numbers("Line 57 failed.", [hit]) == ["57"]


def test_the_results_own_totals_count():
    hit = _rec("R2", [{"record_id": 1}], kind="search", tool="search_logs_tool",
               total_matches=24, returned=1)
    assert unsupported_numbers("24 matching lines, 1 shown.", [hit]) == []


def test_filter_and_argument_values_count():
    counted = _rec("R2", [{"count": 3}], filters={"limit": 50, "start_time": "2017-05-16 00:00:00"})
    assert unsupported_numbers("Top 50 in 2017-05-16 00:00:00 had 3.", [counted]) == []


def test_booleans_are_not_numbers():
    flags = _rec("R2", [{"flag": True}, {"flag": False}])
    assert unsupported_numbers("1 flag set.", [flags]) == ["1"]


def test_a_negative_cell_backs_the_same_magnitude():
    # An offset or a seconds-before value is stored negative; a claim says "after".
    row = _rec("R2", [{"offset": -2, "seconds": -1.5}])
    assert unsupported_numbers("2 lines and 1.5 seconds apart.", [row]) == []


# --- percentages -------------------------------------------------------------


def test_a_percentage_matches_the_same_value():
    assert unsupported_numbers("96.8% of the rows.", [COUNTS]) == []


def test_a_percentage_matches_the_same_fraction():
    assert unsupported_numbers("25% of the rows.", [COUNTS]) == []


def test_a_percentage_the_model_computed_from_counts_fails():
    # 30 of 1999 is 1.5%, but no row says so.
    assert unsupported_numbers("1.5% of the rows.", [COUNTS]) == ["1.5%"]


def test_a_percentage_is_compared_at_the_precision_it_states():
    assert unsupported_numbers("97% of the rows.", [COUNTS]) == []
    assert unsupported_numbers("96.9% of the rows.", [COUNTS]) == ["96.9%"]


# --- dates, times and addresses ----------------------------------------------


def test_a_timestamp_is_matched_as_text_not_as_numbers():
    hit = _rec("R2", [{"record_id": 56, "ts": "2017-05-16 00:00:10.505000+00:00"}],
               kind="search", tool="search_logs_tool")
    assert unsupported_numbers("Line 56 failed at 2017-05-16 00:00:10.", [hit]) == []
    assert unsupported_numbers("Line 56 failed at 2017-05-16 00:00:11.", [hit]) == [
        "2017-05-16 00:00:11"
    ]


def test_a_time_of_day_can_come_from_a_window_filter():
    counted = _rec("R2", [{"count": 3}], filters={"end_time": "2017-05-16 00:15:00"})
    assert unsupported_numbers("3 rows before 00:15.", [counted]) == []


def test_an_address_is_matched_as_text():
    hit = _rec("R2", [{"message": "connect to 10.11.10.1 failed"}], kind="search",
               tool="search_logs_tool")
    assert unsupported_numbers("Connecting to 10.11.10.1 failed.", [hit]) == []
    assert unsupported_numbers("Connecting to 10.11.10.2 failed.", [hit]) == ["10.11.10.2"]


# --- wired into the findings ledger -------------------------------------------


def _answer(**fields):
    return AnswerOutput.model_validate({"blocks": [{"type": "text", "text": "Story."}], **fields})


def test_a_measured_finding_with_a_wrong_number_is_downgraded_to_a_hypothesis():
    answer = _answer(findings=[
        {"id": "F1", "kind": "measured", "statement": "There were 500 ERROR rows.",
         "result_ids": ["R1"]},
    ])
    text = serialize_answer(answer, {"R1"}, [COUNTS])
    assert "**Measured**" not in text
    assert "**Hypotheses**" in text
    assert "There were 500 ERROR rows. (not shown by the cited result: 500)" in text


def test_a_measured_finding_with_backed_numbers_stays_measured():
    answer = _answer(findings=[
        {"id": "F1", "kind": "measured", "statement": "30 ERROR rows, 1,969 WARNING rows.",
         "result_ids": ["R1"]},
    ])
    text = serialize_answer(answer, {"R1"}, [COUNTS])
    assert "**Measured**" in text and "**Hypotheses**" not in text


def test_hypotheses_and_limitations_may_contain_any_number():
    answer = _answer(findings=[
        {"id": "F1", "kind": "hypothesis", "statement": "Maybe 500 hosts are affected.",
         "result_ids": ["R1"]},
        {"id": "F2", "kind": "limitation", "statement": "Only 3 days were searched."},
    ])
    text = serialize_answer(answer, {"R1"}, [COUNTS])
    assert "not shown by the cited result" not in text


def test_the_number_may_come_from_any_of_the_cited_results():
    hit = _rec("R2", [{"record_id": 56}], kind="search", tool="search_logs_tool")
    answer = _answer(findings=[
        {"id": "F1", "kind": "measured", "statement": "Line 56 sits in a service with 30 errors.",
         "result_ids": ["R1", "R2"]},
    ])
    text = serialize_answer(answer, {"R1", "R2"}, [COUNTS, hit])
    assert "**Measured**" in text


def test_a_number_only_in_an_uncited_result_does_not_count():
    other = _rec("R2", [{"count": 500}])
    answer = _answer(findings=[
        {"id": "F1", "kind": "measured", "statement": "There were 500 ERROR rows.",
         "result_ids": ["R1"]},
    ])
    text = serialize_answer(answer, {"R1", "R2"}, [COUNTS, other])
    assert "not shown by the cited result: 500" in text


def test_without_records_the_check_cannot_run_and_leaves_the_finding_alone():
    answer = _answer(findings=[
        {"id": "F1", "kind": "measured", "statement": "There were 500 ERROR rows.",
         "result_ids": ["R1"]},
    ])
    assert "**Measured**" in serialize_answer(answer, {"R1"})
