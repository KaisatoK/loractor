"""Gated live checks for the semantic claim check (Jev) and its revision policy.

Offline, only case-data validation runs. Run this module alone with
``bash ./claims.sh`` so the real TypeSafe key is loaded before imports.

Every case is a measured claim plus the results it cites. The cases are built so
the *code* number check passes (all figures are in the cited rows): what is under
test is the semantic judgment the code cannot make -- the right number on the
wrong entity, a rise that is really a fall, a claim the rows do not cover. A case
that flips is a product signal: re-run once, then decide whether the input or the
product is wrong; never re-baseline an expectation to match the output.
"""

from __future__ import annotations

import html
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

RUN_LIVE = os.environ.get("RUN_LIVE_CLAIMS") == "1"
if RUN_LIVE:
    from dotenv import load_dotenv

    load_dotenv(override=True)
else:
    # Offline the classifier is built but never called.
    os.environ.setdefault("TYPESAFE_API_KEY", "test-key")

import pytest

from backend.pipeline.claims import ClaimTarget, unsupported_numbers
from backend.pipeline.decision import (
    classifier,
    decide_claim_revision,
    decide_claims,
)
from backend.pipeline.prompts import PipelinePrompts

prompts = PipelinePrompts()

_WINDOW = {"start_time": "2017-05-16 00:00:00", "end_time": "2017-05-16 01:00:00"}


def _result(rid, rows, kind="aggregate", tool="count_by_category_tool", **extra):
    return {
        "id": rid, "tool_name": tool, "arguments": {}, "row_count": len(rows),
        "rows": rows, "kind": kind, "dataset": "openstack", "query": "SELECT 1",
        "filters": dict(_WINDOW), "truncated": False, **extra,
    }


def _trend(rid, counts):
    rows = [{"bucket": f"2017-05-16 00:{10 * i:02d}:00", "count": n} for i, n in enumerate(counts)]
    return _result(rid, rows, kind="trend", tool="get_trend_tool")


ERRORS_BY_SERVICE = _result("R1", [
    {"service": "alpha", "severity": "ERROR", "count": 30},
    {"service": "beta", "severity": "ERROR", "count": 4},
])
SEVERITIES_OF_ALPHA = _result("R1", [
    {"service": "alpha", "severity": "WARNING", "count": 30},
    {"service": "alpha", "severity": "ERROR", "count": 4},
])
EXAMPLES = _result("R1", [
    {"record_id": 101, "ts": "2017-05-16 00:03:10", "severity": "ERROR", "service": "alpha",
     "message": "connection reset by peer"},
    {"record_id": 202, "ts": "2017-05-16 00:41:55", "severity": "ERROR", "service": "alpha",
     "message": "connection reset by peer"},
], kind="search", tool="search_logs_tool", total_matches=2)


@dataclass(frozen=True)
class ClaimCase:
    id: str
    claim: str
    result: dict
    expected_relation: str
    expected_action: str


CASES = [
    ClaimCase(
        "supports_a_count",
        "alpha logged 30 ERROR rows in the window.",
        ERRORS_BY_SERVICE, "supports", "keep",
    ),
    ClaimCase(
        "supports_a_rise",
        "The count rose from 2 in the first bucket to 14 in the last.",
        _trend("R1", [2, 3, 5, 9, 14]), "supports", "keep",
    ),
    ClaimCase(
        "contradicts_the_wrong_entity",
        "beta logged 30 ERROR rows in the window.",
        ERRORS_BY_SERVICE, "contradicts", "contradicted",
    ),
    ClaimCase(
        "contradicts_the_wrong_severity",
        "alpha logged 30 ERROR rows in the window.",
        SEVERITIES_OF_ALPHA, "contradicts", "contradicted",
    ),
    ClaimCase(
        "contradicts_the_direction",
        "The count rose from 2 in the first bucket to 14 in the last.",
        _trend("R1", [14, 9, 5, 3, 2]), "contradicts", "contradicted",
    ),
    # A claim the rows do not speak to at all. Two earlier drafts were not that:
    # "gamma logged ERROR rows" against a per-service listing that omits gamma, and
    # "resets happen every few minutes" against two examples 38 minutes apart. Each
    # has a defensible "contradicts" reading (an exhaustive listing implies absence;
    # the gap contradicts the pace), and the live run split its probability between
    # the two options (confidence 0.31-0.35, below the gate, so the finding was
    # correctly left alone). The inputs were ambiguous, so they were replaced by
    # claims about something the rows do not contain. A third draft, "alpha's
    # latency was normal", was vague in the same way ("normal" names no baseline the
    # rows could meet or miss); it scored says_nothing 0.61, confidence 0.41.
    ClaimCase(
        "says_nothing_about_an_attribute_the_rows_lack",
        "All of alpha's ERROR rows came from a single host.",
        ERRORS_BY_SERVICE, "says_nothing", "relabel",
    ),
    ClaimCase(
        "says_nothing_when_examples_stand_for_a_ranking",
        "Connection resets are the most common error in the dataset.",
        EXAMPLES, "says_nothing", "relabel",
    ),
]

_REPORT: list[dict] = []
_ANNOUNCED = False


def _report_path() -> Path:
    return Path(os.environ.get("RUN_LIVE_CLAIMS_REPORT", ".data/claims_report.html"))


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
            row.get("relation", "not run"),
            row.get("expected_relation", ""),
            row.get("action", "not run"),
            row.get("expected_action", ""),
            row.get("revision", ""),
            "✓" if row.get("ok") else "✗",
            json.dumps(row.get("probabilities", {}), sort_keys=True),
            row.get("confidence", ""),
        ]
        rows.append("<tr>" + "".join(f"<td>{html.escape(str(v))}</td>" for v in values) + "</tr>")
    body = "<html><head><meta charset='utf-8'><title>Claim check report</title></head><body>"
    body += f"<h1>Claim check report</h1><p>Generated {datetime.now(timezone.utc).isoformat()}</p>"
    body += (
        "<table border='1'><tr><th>case</th><th>relation</th><th>expected relation</th>"
        "<th>action</th><th>expected action</th><th>revision route</th><th>ok</th>"
        "<th>probabilities</th><th>confidence</th></tr>"
    )
    body += "".join(rows) + "</table></body></html>"
    path.write_text(body, encoding="utf-8")
    if not _ANNOUNCED:
        _ANNOUNCED = True
        print(f"HTML report: {path.resolve().as_uri()}")


def _target(case: ClaimCase) -> ClaimTarget:
    return ClaimTarget(0, "F1", case.claim, (case.result,))


@pytest.mark.skipif(not RUN_LIVE, reason="live claim check — run with bash ./claims.sh")
@pytest.mark.parametrize("case", CASES, ids=lambda case: case.id)
def test_live_claim_check(case: ClaimCase):
    target = _target(case)
    response = classifier.invoke(prompts.claims_request([target]))
    (check,) = decide_claims(response, [target])

    # A contradicted claim must send an otherwise finished answer back.
    route = decide_claim_revision(
        {"next_state": "end", "revision_reason": "none", "signals": {}}, [check], "High", 0
    )
    revision = f"{route['next_state']}/{route['revision_reason']}"
    expected_revision = (
        "answer/contradicted_claim" if case.expected_action == "contradicted" else "end/none"
    )

    row = {
        "id": case.id,
        "relation": check["relation"],
        "expected_relation": case.expected_relation,
        "action": check["action"],
        "expected_action": case.expected_action,
        "revision": revision,
        "probabilities": check["probabilities"],
        "confidence": check["confidence"],
        "ok": (
            check["relation"] == case.expected_relation
            and check["action"] == case.expected_action
            and revision == expected_revision
        ),
    }
    _REPORT.append(row)
    _write_report()
    assert row["ok"], (
        f"{case.id}: expected {case.expected_relation}/{case.expected_action}/"
        f"{expected_revision}, got {check['relation']}/{check['action']}/{revision}; "
        f"confidence={check['confidence']} probabilities={check['probabilities']}"
    )


def test_case_data_is_valid():
    ids = [case.id for case in CASES]
    assert len(ids) == len(set(ids))
    assert {case.expected_relation for case in CASES} == {"supports", "contradicts", "says_nothing"}
    action_of = {"supports": "keep", "contradicts": "contradicted", "says_nothing": "relabel"}
    assert all(case.expected_action == action_of[case.expected_relation] for case in CASES)


def test_every_case_reaches_the_semantic_check():
    # The code number check must pass, otherwise the claim never goes to Jev and
    # the case would test the wrong layer.
    for case in CASES:
        assert unsupported_numbers(case.claim, [case.result]) == [], case.id
