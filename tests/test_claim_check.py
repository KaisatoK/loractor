"""Offline tests for the semantic claim check (a fake classifier stands in for Jev)."""

import os

os.environ.setdefault("TYPESAFE_API_KEY", "test-key")

import pytest
from langchain_typesafe import ChoiceAnswer, ClassifierResponse, NoulAnswer

from backend.pipeline import decision, graph
from backend.pipeline.answers import serialize_answer
from backend.pipeline.claims import (
    MAX_CHECKED_CLAIMS,
    MAX_CLAIM_ROWS,
    claim_targets,
    relevant_rows,
)
from backend.pipeline.contracts import AnswerOutput
from backend.pipeline.prompts import PipelinePrompts

prompts = PipelinePrompts()


def _rec(rid, rows, kind="aggregate", tool="count_by_category_tool", **extra):
    return {
        "id": rid, "tool_name": tool, "arguments": {}, "row_count": len(rows),
        "rows": rows, "kind": kind, "dataset": "openstack", "query": "SELECT 1",
        "filters": {"start_time": "2017-05-16 00:00:00", "end_time": "2017-05-16 01:00:00"},
        "truncated": False, **extra,
    }


COUNTS = _rec("R1", [
    {"service": "alpha", "severity": "ERROR", "count": 30},
    {"service": "beta", "severity": "ERROR", "count": 4},
])


def _finding(fid, kind, statement, ids=("R1",)):
    return {"id": fid, "kind": kind, "statement": statement, "result_ids": list(ids)}


def _answer(*findings):
    return AnswerOutput.model_validate(
        {"blocks": [{"type": "text", "text": "Story."}], "findings": list(findings)}
    )


def _choice(relation, confidence):
    others = [o for o in ("supports", "contradicts", "says_nothing") if o != relation]
    return ChoiceAnswer(
        type="choice", choice=relation, confidence=confidence,
        probabilities={relation: 0.9, others[0]: 0.06, others[1]: 0.04},
    )


class FakeClassifier:
    """Answers each claim question with a fixed relation, recording the request."""

    def __init__(self, relations=None, default=("supports", 0.95), fail=False):
        self.relations = relations or {}
        self.default = default
        self.fail = fail
        self.requests = []

    def invoke(self, request):
        self.requests.append(request)
        if self.fail:
            raise RuntimeError("service down")
        answers = {}
        for key, question in request["questions"].items():
            if question.type == "choice":
                relation, confidence = self.relations.get(key, self.default)
                answers[key] = _choice(relation, confidence)
            else:  # the confidence questions: nothing to revise, nothing to ask
                value = 0.9 if key in {
                    "evidence_relevant", "answer_grounded", "answer_fits_role",
                    "answer_complete",
                } else 0.05
                answers[key] = NoulAnswer(type="noul", noul=value)
        return ClassifierResponse(model="fake", answers=answers)


# --- which claims are checked -------------------------------------------------


def test_only_measured_findings_that_cite_a_real_result_are_checked():
    answer = _answer(
        _finding("F1", "measured", "alpha logged 30 ERROR rows."),
        _finding("F2", "hypothesis", "Maybe 99 hosts are affected."),
        _finding("F3", "limitation", "Only one hour was counted."),
        _finding("F4", "measured", "beta logged 4 ERROR rows.", ids=("R9",)),
        _finding("F5", "measured", "beta logged 4 ERROR rows.", ids=()),
    )
    targets = claim_targets(answer, {"R1"}, [COUNTS])
    assert [(t.index, t.finding_id) for t in targets] == [(0, "F1")]
    assert targets[0].key == "claim_0"
    assert [r["id"] for r in targets[0].records] == ["R1"]


def test_a_claim_with_a_number_no_result_shows_is_not_sent_to_the_model():
    answer = _answer(_finding("F1", "measured", "alpha logged 500 ERROR rows."))
    assert claim_targets(answer, {"R1"}, [COUNTS]) == []


def test_nothing_is_checked_without_records():
    answer = _answer(_finding("F1", "measured", "alpha logged 30 ERROR rows."))
    assert claim_targets(answer, {"R1"}, []) == []


def test_the_check_is_capped():
    rows = [{"service": f"s{i}", "count": i + 1} for i in range(20)]
    big = _rec("R1", rows)
    findings = [_finding(f"F{i}", "measured", f"s{i} has {i + 1} rows.") for i in range(20)]
    targets = claim_targets(_answer(*findings), {"R1"}, [big])
    assert len(targets) == MAX_CHECKED_CLAIMS


def test_a_dict_answer_works_too():
    answer = {"findings": [_finding("F1", "measured", "alpha logged 30 ERROR rows.")]}
    assert len(claim_targets(answer, {"R1"}, [COUNTS])) == 1


# --- which rows a claim sees --------------------------------------------------


def test_a_short_result_is_shown_whole():
    rows = [{"n": i} for i in range(MAX_CLAIM_ROWS)]
    assert relevant_rows("anything", rows) == rows


def test_a_long_result_keeps_the_rows_the_claim_talks_about_in_order():
    rows = [{"service": f"svc{i:03d}", "count": 1000 + i} for i in range(100)]
    shown = relevant_rows("svc077 had 1077 rows and svc012 had 1012.", rows)
    assert len(shown) == MAX_CLAIM_ROWS
    assert {"service": "svc012", "count": 1012} in shown
    assert {"service": "svc077", "count": 1077} in shown
    assert shown == sorted(shown, key=lambda r: r["service"])  # original order


def test_with_no_matching_row_the_first_rows_are_kept():
    rows = [{"service": f"svc{i:03d}", "count": 1000 + i} for i in range(100)]
    assert relevant_rows("nothing in common", rows) == rows[:MAX_CLAIM_ROWS]


def test_the_row_that_settles_a_claim_is_never_cut_by_position():
    rows = [{"service": f"svc{i:03d}", "count": i} for i in range(100)]
    rows[90] = {"service": "gamma", "count": 4242}
    shown = relevant_rows("gamma had 4242 rows.", rows)
    assert {"service": "gamma", "count": 4242} in shown


# --- the request --------------------------------------------------------------


def test_one_choice_question_per_claim_in_one_request():
    answer = _answer(
        _finding("F1", "measured", "alpha logged 30 ERROR rows."),
        _finding("F2", "measured", "beta logged 4 ERROR rows."),
    )
    targets = claim_targets(answer, {"R1"}, [COUNTS])
    request = prompts.claims_request(targets)

    assert set(request["questions"]) == {"claim_0", "claim_1"}
    assert set(request["state"]["findings"]) == {"claim_0", "claim_1"}
    for key, question in request["questions"].items():
        assert question.type == "choice"
        assert set(question.criteria) == {"supports", "contradicts", "says_nothing"}
        # The question names its own claim, not another one.
        assert f"findings.{key}.claim" in str(question.instructions)
        assert f"findings.{key}.cited_results" in str(question.instructions)
        for option in question.criteria.values():
            assert "what" in option and "not_for" in option


def test_the_state_carries_the_claim_scope_and_rows_with_their_numbers():
    answer = _answer(_finding("F1", "measured", "alpha logged 30 ERROR rows."))
    request = prompts.claims_request(claim_targets(answer, {"R1"}, [COUNTS]))
    entry = request["state"]["findings"]["claim_0"]
    assert entry["claim"] == "alpha logged 30 ERROR rows."
    cited = entry["cited_results"][0]
    assert cited["id"] == "R1" and cited["kind"] == "aggregate"
    assert cited["filters"]["start_time"] == "2017-05-16 00:00:00"
    assert cited["row_count"] == 2 and cited["rows_shown"] == 2
    assert cited["rows"][0] == {"service": "alpha", "severity": "ERROR", "count": 30}


def test_a_long_cell_is_cut_with_a_marker_in_the_request():
    hit = _rec("R2", [{"record_id": 5, "message": "x" * 1000}], kind="search",
               tool="search_logs_tool")
    answer = _answer(_finding("F1", "measured", "Line 5 failed.", ids=("R2",)))
    request = prompts.claims_request(claim_targets(answer, {"R2"}, [hit]))
    message = request["state"]["findings"]["claim_0"]["cited_results"][0]["rows"][0]["message"]
    assert message.endswith("…[cut]") and len(message) < 400


# --- the policy ---------------------------------------------------------------


def _targets(n=1):
    findings = [_finding(f"F{i + 1}", "measured", "alpha logged 30 ERROR rows.") for i in range(n)]
    return claim_targets(_answer(*findings), {"R1"}, [COUNTS])


@pytest.mark.parametrize(
    ("relation", "confidence", "action"),
    [
        ("supports", 0.95, "keep"),
        ("contradicts", 0.95, "contradicted"),
        ("says_nothing", 0.95, "relabel"),
        ("contradicts", 0.8, "contradicted"),  # the gate is inclusive
        ("contradicts", 0.79, "leave"),
        ("says_nothing", 0.5, "leave"),
        ("supports", 0.3, "leave"),
    ],
)
def test_the_gate_and_the_action_per_relation(relation, confidence, action):
    response = FakeClassifier(default=(relation, confidence)).invoke(
        prompts.claims_request(_targets())
    )
    (check,) = decision.decide_claims(response, _targets())
    assert check["action"] == action
    assert check["relation"] == relation and check["confidence"] == confidence
    assert check["finding_id"] == "F1" and check["cited"] == ["R1"]
    assert check["probabilities"][relation] == 0.9


def test_a_missing_answer_leaves_the_finding_alone():
    empty = ClassifierResponse(model="fake", answers={})
    (check,) = decision.decide_claims(empty, _targets())
    assert check["action"] == "leave" and check["relation"] is None


def test_only_relabel_and_contradicted_change_the_ledger():
    checks = [
        {"index": 0, "action": "keep"}, {"index": 1, "action": "leave"},
        {"index": 2, "action": "relabel"}, {"index": 3, "action": "contradicted"},
    ]
    assert set(decision.claim_verdicts(checks)) == {2, 3}


def test_contradicted_claims_send_the_answer_back_naming_them():
    route = {"next_state": "end", "revision_reason": "none", "signals": {}}
    checks = [
        {"action": "keep", "finding_id": "F1", "statement": "s1", "cited": ["R1"]},
        {"action": "contradicted", "finding_id": "F2", "statement": "beta had 30", "cited": ["R1"]},
    ]
    revised = decision.decide_claim_revision(route, checks, "High", 0)
    assert revised["next_state"] == "answer"
    assert revised["revision_reason"] == "contradicted_claim"
    assert [c["finding_id"] for c in revised["contradicted"]] == ["F2"]
    feedback = decision.claim_feedback(revised["contradicted"])
    assert "F2: beta had 30 (cites R1)" in feedback and "F1" not in feedback


def test_an_evidence_revision_is_not_overridden():
    route = {"next_state": "find_evidence", "revision_reason": "missing_evidence"}
    checks = [{"action": "contradicted", "finding_id": "F1", "statement": "s", "cited": ["R1"]}]
    assert decision.decide_claim_revision(route, checks, "High", 0) is route


def test_a_spent_revision_budget_ends_the_run():
    route = {"next_state": "end", "revision_reason": "none"}
    checks = [{"action": "contradicted", "finding_id": "F1", "statement": "s", "cited": ["R1"]}]
    assert decision.decide_claim_revision(route, checks, "Low", 0) is route  # budget 0
    assert decision.decide_claim_revision(route, checks, "Medium", 1) is route  # budget 1
    assert decision.decide_claim_revision(route, checks, "Medium", 0)["next_state"] == "answer"


def test_no_contradiction_leaves_the_route_alone():
    route = {"next_state": "end", "revision_reason": "none"}
    checks = [{"action": "relabel", "finding_id": "F1", "statement": "s", "cited": ["R1"]}]
    assert decision.decide_claim_revision(route, checks, "High", 0) is route
    assert decision.decide_claim_revision(route, [], "High", 0) is route


# --- the ledger ---------------------------------------------------------------


def test_a_relabelled_finding_is_shown_as_a_hypothesis_with_the_reason():
    answer = _answer(_finding("F1", "measured", "alpha logged 30 ERROR rows."))
    verdicts = {0: {"action": "relabel"}}
    text = serialize_answer(answer, {"R1"}, [COUNTS], claim_verdicts=verdicts)
    assert "**Measured**" not in text
    assert "alpha logged 30 ERROR rows. (the cited result does not address this claim)" in text


def test_a_contradicted_finding_says_so():
    answer = _answer(_finding("F1", "measured", "alpha logged 30 ERROR rows."))
    text = serialize_answer(answer, {"R1"}, [COUNTS], claim_verdicts={0: {"action": "contradicted"}})
    assert "(the cited result contradicts this claim)" in text and "**Measured**" not in text


def test_a_verdict_leaves_the_other_findings_alone():
    answer = _answer(
        _finding("F1", "measured", "alpha logged 30 ERROR rows."),
        _finding("F2", "measured", "beta logged 4 ERROR rows."),
    )
    text = serialize_answer(answer, {"R1"}, [COUNTS], claim_verdicts={0: {"action": "relabel"}})
    assert "**Measured**" in text and "**F2** beta logged 4 ERROR rows." in text


# --- answer node ---------------------------------------------------------------


def _state(**extra):
    return {
        "role": {"name": "team_lead", "objective": "o"},
        "effort": "High",
        "normalized": {"normalized_question": "q"},
        "evidence_report": {},
        "tool_results": [COUNTS],
        **extra,
    }


def _patch(monkeypatch, classifier, answer):
    monkeypatch.setattr(graph, "classifier", classifier)
    monkeypatch.setattr(graph, "generate_answer", lambda prompt: answer)


def test_answer_node_relabels_and_records_a_claim_the_results_do_not_address(monkeypatch):
    fake = FakeClassifier(default=("says_nothing", 0.93))
    _patch(monkeypatch, fake, _answer(_finding("F1", "measured", "alpha logged 30 ERROR rows.")))
    result = graph.answer_node(_state())
    assert len(fake.requests) == 1
    assert [c["action"] for c in result["claim_checks"]] == ["relabel"]
    assert "(the cited result does not address this claim)" in result["answer"]
    assert "**Measured**" not in result["answer"]


def test_answer_node_keeps_a_supported_claim_as_measured(monkeypatch):
    _patch(monkeypatch, FakeClassifier(), _answer(_finding("F1", "measured", "alpha logged 30 ERROR rows.")))
    result = graph.answer_node(_state())
    assert "**Measured**" in result["answer"]
    assert [c["action"] for c in result["claim_checks"]] == ["keep"]


def test_answer_node_leaves_a_low_confidence_judgment_alone(monkeypatch):
    fake = FakeClassifier(default=("contradicts", 0.5))
    _patch(monkeypatch, fake, _answer(_finding("F1", "measured", "alpha logged 30 ERROR rows.")))
    result = graph.answer_node(_state())
    assert "**Measured**" in result["answer"]
    assert [c["action"] for c in result["claim_checks"]] == ["leave"]


def test_answer_node_does_not_call_the_classifier_without_a_measured_claim(monkeypatch):
    fake = FakeClassifier()
    _patch(monkeypatch, fake, _answer(_finding("F1", "hypothesis", "Maybe a disk problem.")))
    result = graph.answer_node(_state())
    assert fake.requests == [] and result["claim_checks"] == []


def test_a_classifier_failure_keeps_the_findings_as_labelled(monkeypatch):
    fake = FakeClassifier(fail=True)
    _patch(monkeypatch, fake, _answer(_finding("F1", "measured", "alpha logged 30 ERROR rows.")))
    result = graph.answer_node(_state())
    assert len(fake.requests) == 1
    assert "**Measured**" in result["answer"] and result["claim_checks"] == []


# --- confidence node ------------------------------------------------------------


def _confidence_state(checks, revision_count=0, effort="High"):
    return _state(
        answer="draft", effort=effort, revision_count=revision_count,
        claim_checks=checks, tool_calls=[],
    )


CONTRADICTED = [{
    "index": 0, "finding_id": "F1", "statement": "beta logged 30 ERROR rows.",
    "cited": ["R1"], "relation": "contradicts", "confidence": 0.97,
    "probabilities": {}, "action": "contradicted",
}]


def test_a_contradicted_claim_triggers_a_revision_with_the_claim_in_the_feedback(monkeypatch):
    monkeypatch.setattr(graph, "classifier", FakeClassifier())
    result = graph.confidence_node(_confidence_state(CONTRADICTED))
    assert result["confidence"]["next_state"] == "answer"
    assert result["confidence"]["revision_reason"] == "contradicted_claim"
    assert result["revision_count"] == 1
    assert "F1: beta logged 30 ERROR rows." in result["revision_feedback"]
    assert graph.route_after_confidence(result) == "answer"


def test_the_revision_budget_still_applies(monkeypatch):
    monkeypatch.setattr(graph, "classifier", FakeClassifier())
    spent = graph.confidence_node(_confidence_state(CONTRADICTED, revision_count=2))
    low = graph.confidence_node(_confidence_state(CONTRADICTED, effort="Low"))
    assert spent["confidence"]["next_state"] == "end" == low["confidence"]["next_state"]
    assert "revision_count" not in low or low["revision_count"] == 0


def test_without_a_contradiction_the_confidence_route_is_unchanged(monkeypatch):
    monkeypatch.setattr(graph, "classifier", FakeClassifier())
    relabel = [{**CONTRADICTED[0], "action": "relabel", "relation": "says_nothing"}]
    for checks in ([], relabel):
        result = graph.confidence_node(_confidence_state(checks))
        assert result["confidence"]["next_state"] == "end"
        assert result["confidence"]["revision_reason"] == "none"


def test_a_failed_confidence_classifier_does_not_hide_a_contradiction(monkeypatch):
    monkeypatch.setattr(graph, "classifier", FakeClassifier(fail=True))
    result = graph.confidence_node(_confidence_state(CONTRADICTED))
    assert result["confidence"]["revision_reason"] == "contradicted_claim"


def test_a_revision_rechecks_the_new_answer(monkeypatch):
    # First draft: contradicted. After the revision the new draft is supported.
    fake = FakeClassifier(default=("contradicts", 0.97))
    _patch(monkeypatch, fake, _answer(_finding("F1", "measured", "beta logged 30 ERROR rows.")))
    first = graph.answer_node(_state())
    assert [c["action"] for c in first["claim_checks"]] == ["contradicted"]

    fake.default = ("supports", 0.97)
    monkeypatch.setattr(
        graph, "generate_answer",
        lambda prompt: _answer(_finding("F1", "measured", "alpha logged 30 ERROR rows.")),
    )
    second = graph.answer_node({**first, "revision_count": 1})
    assert [c["action"] for c in second["claim_checks"]] == ["keep"]
    assert "**Measured**" in second["answer"]
