"""``rank_priorities_tool``: schema, per-dataset scoring and the server tool.

The tool is offered on the MCP server only (an external agent has no
``answer_node``), so its scoring lives in ``pipeline/ranking.py`` -- arithmetic
over count rows, tested here with synthetic rows -- and its data fetching in
``specs._run_rank_priorities``, tested through the in-memory ``fastmcp.Client``
with the DB functions replaced.
"""

from __future__ import annotations

import asyncio

import pytest
from fastmcp import Client
from pydantic import ValidationError

from backend.db.capture import record_query
from backend.db.errors import Refusal
from backend.db.stats import QueryResult, QueryStats
from backend.mcp_server import server
from backend.pipeline import priority, ranking
from backend.pipeline.prompts.builders import TOOL_CATALOG
from backend.pipeline.tools import specs
from backend.pipeline.tools.local import LOCAL_TOOLS
from backend.pipeline.tools.mcp_tools import MCP_TOOLS
from backend.pipeline.tools.schemas import RankPriorities

BASELINE_START = "2017-05-16 00:00:00"
BASELINE_END = "2017-05-16 12:00:00"
RECENT_START = "2017-05-16 12:00:00"
RECENT_END = "2017-05-17 00:00:00"

VALID = dict(
    baseline_start=BASELINE_START,
    baseline_end=BASELINE_END,
    recent_start=RECENT_START,
    recent_end=RECENT_END,
)


# --- Task 3: the argument model -------------------------------------------------
def test_defaults_are_service_ten_rows_and_every_local_dataset():
    model = RankPriorities(**VALID)
    assert model.datasets is None
    assert model.group_by == "service"
    assert model.limit == 10


def test_datasets_are_registry_ids_deduplicated():
    assert RankPriorities(datasets=["openstack", "openstack"], **VALID).datasets == [
        "openstack"
    ]
    # A remote dataset is accepted for a service ranking.
    assert RankPriorities(datasets=["otel_s3"], **VALID).datasets == ["otel_s3"]


def test_an_unknown_dataset_is_rejected():
    with pytest.raises(ValidationError, match="Unknown dataset"):
        RankPriorities(datasets=["openstack_normal1"], **VALID)


def test_windows_must_be_ordered_and_must_not_overlap():
    with pytest.raises(ValidationError, match="baseline_end must be after"):
        RankPriorities(
            baseline_start=BASELINE_END, baseline_end=BASELINE_START,
            recent_start=RECENT_START, recent_end=RECENT_END,
        )
    with pytest.raises(ValidationError, match="recent_end must be after"):
        RankPriorities(
            baseline_start=BASELINE_START, baseline_end=BASELINE_END,
            recent_start=RECENT_END, recent_end=RECENT_START,
        )
    with pytest.raises(ValidationError, match="must not overlap"):
        RankPriorities(
            baseline_start=BASELINE_START, baseline_end="2017-05-16 18:00:00",
            recent_start=RECENT_START, recent_end=RECENT_END,
        )
    # Adjacent windows are the intended use: end == start is not an overlap.
    RankPriorities(**VALID)


def test_team_ranking_is_refused_for_a_remote_dataset_with_a_suggestion():
    with pytest.raises(ValidationError, match="use group_by='service' for otel_s3"):
        RankPriorities(datasets=["otel_s3"], group_by="team", **VALID)
    # ... and accepted for local datasets.
    RankPriorities(datasets=["openstack"], group_by="team", **VALID)


def test_limit_is_bounded():
    assert RankPriorities(limit=50, **VALID).limit == 50
    for bad in (0, 51):
        with pytest.raises(ValidationError):
            RankPriorities(limit=bad, **VALID)


# --- Task 4: per-dataset scoring -------------------------------------------------
def _rows(dimension, *triples):
    return [
        {dimension: value, "severity": severity, "count": count}
        for value, severity, count in triples
    ]


# A worked example, checked by hand.
BASELINE = _rows(
    "service",
    ("a", "ERROR", 10),
    ("a", "WARNING", 30),
    ("b", "ERROR", 5),
    ("b", "INFO", 100),
)
RECENT = _rows(
    "service",
    ("a", "ERROR", 20),
    ("a", "WARNING", 10),
    ("b", "ERROR", 15),
    ("b", "INFO", 10),
)


def test_shares_and_trend_match_a_hand_calculation():
    result = ranking.rank_dataset("openstack", "service", BASELINE, RECENT)
    # Counts are baseline + recent. non-INFO: a = 70, b = 20; total = 90.
    # volume_share: a 77.8%, b 22.2%. error_share: a 30/70 = 42.9%, b 20/20 = 100%.
    # trend: a (30-40)/40 -> 0, b (15-5)/5 -> 1.
    # score a = (0.4*0.7778+0.3*0.4286+0.2*0)/0.9 = 0.489
    # score b = (0.4*0.2222+0.3*1.0+0.2*1.0)/0.9  = 0.654
    by_name = {row["service"]: row for row in result["rows"]}
    assert [row["service"] for row in result["rows"]] == ["b", "a"]
    assert by_name["a"]["non_info_rows"] == 70 and by_name["a"]["error_rows"] == 30
    assert by_name["a"]["volume_share_pct"] == 77.8
    assert by_name["a"]["error_share_pct"] == 42.9
    assert by_name["a"]["trend_up"] == 0.0
    assert by_name["b"]["volume_share_pct"] == 22.2
    assert by_name["b"]["error_share_pct"] == 100.0
    assert by_name["b"]["trend_up"] == 1.0
    assert by_name["b"]["score"] == 0.654
    assert result["ranked"] == 2
    assert "breadth" in " ".join(result["notes"])
    assert result["terms_used"] == ["volume_share", "error_share", "trend_up"]


def test_each_dataset_has_its_own_denominator():
    combined = ranking.rank_all(
        [
            {
                "dataset": "openstack",
                "baseline_rows": _rows("service", ("a", "ERROR", 10)),
                "recent_rows": _rows("service", ("a", "ERROR", 10)),
            },
            {
                "dataset": "bgl",
                "baseline_rows": _rows("service", ("z", "ERROR", 1000)),
                "recent_rows": _rows("service", ("z", "ERROR", 1000)),
            },
        ],
        dimension="service",
    )
    # One value per dataset: each holds 100% of its own dataset's volume.
    shares = {row["dataset"]: row["volume_share_pct"] for row in combined["rows"]}
    assert shares == {"openstack": 100.0, "bgl": 100.0}
    assert [(r["dataset"], r["rank"]) for r in combined["rows"]] == [
        ("bgl", 1), ("openstack", 1),
    ]


def test_info_rows_are_excluded_and_case_does_not_matter():
    rows_baseline = _rows(
        "service",
        ("a", "info", 90),      # OTel lower case: informational, excluded
        ("a", "error", 10),
        ("b", "INFORMATION", 50),  # also informational
    )
    result = ranking.rank_dataset("otel_s3", "service", rows_baseline, [])
    assert [row["service"] for row in result["rows"]] == ["a"]
    assert result["rows"][0]["non_info_rows"] == 10
    assert result["rows"][0]["error_rows"] == 10


def test_a_value_seen_in_only_one_window_is_ranked_or_dropped_by_the_other_cut():
    only_baseline = _rows("service", ("a", "ERROR", 10))
    # The recent window was not cut, so a's recent count there really is 0.
    uncut = ranking.rank_dataset("openstack", "service", only_baseline, [])
    assert [row["service"] for row in uncut["rows"]] == ["a"]
    assert uncut["ranked"] == 1
    # The recent window hit its group cap, so a's count there is unknown.
    cut = ranking.rank_dataset(
        "openstack", "service", only_baseline, [], recent_cut=True
    )
    assert cut["rows"] == [] and cut["ranked"] == 0
    assert any("dropped 1 value" in note for note in cut["notes"])
    assert any(f"{ranking.CANDIDATE_GROUPS}-group cap" in note for note in cut["notes"])


def test_limit_caps_the_rows_but_not_the_reported_total():
    baseline = _rows(
        "service", ("a", "ERROR", 30), ("b", "ERROR", 20), ("c", "ERROR", 10)
    )
    result = ranking.rank_dataset("openstack", "service", baseline, [], limit=2)
    assert len(result["rows"]) == 2
    assert result["ranked"] == 3


def test_service_rows_are_tagged_with_the_owning_team():
    result = ranking.rank_dataset(
        "openstack", "service", BASELINE, RECENT, team_for=lambda c: "T:" + c
    )
    assert {row["team"] for row in result["rows"]} == {"T:a", "T:b"}


def test_team_mode_does_not_repeat_the_dimension_as_a_team_column():
    rows = _rows("team", ("Compute", "ERROR", 10), ("Storage", "ERROR", 5))
    result = ranking.rank_dataset(
        "openstack", "team", rows, [], team_for=lambda c: "ignored"
    )
    assert [row["team"] for row in result["rows"]] == ["Compute", "Storage"]


def test_a_dataset_with_no_problem_rows_is_a_note_not_a_crash():
    rows = _rows("service", ("a", "INFO", 100))
    result = ranking.rank_dataset("openstack", "service", rows, rows)
    assert result["rows"] == [] and result["ranked"] == 0
    assert any("no non-informational rows" in note for note in result["notes"])


def test_rank_all_carries_the_formula_and_flags_remote_approximation():
    combined = ranking.rank_all(
        [
            {
                "dataset": "otel_s3",
                "baseline_rows": _rows("service", ("a", "error", 10)),
                "recent_rows": _rows("service", ("a", "error", 10)),
                "approximate": True,
            }
        ],
        dimension="service",
    )
    assert combined["rows"][0]["dataset"] == "otel_s3"
    assert any(priority.FORMULA in note for note in combined["notes"])
    assert any("approximate" in note for note in combined["notes"])


# --- Task 5: the tool through the in-memory client -------------------------------
def _call(name: str, arguments: dict):
    async def run():
        async with Client(server.mcp) as client:
            return await client.call_tool(name, arguments, raise_on_error=False)

    return asyncio.run(run())


def test_the_tool_is_server_only():
    assert "rank_priorities_tool" not in LOCAL_TOOLS
    assert "rank_priorities_tool" not in MCP_TOOLS
    assert "rank_priorities_tool" not in TOOL_CATALOG
    assert specs.BY_NAME["rank_priorities_tool"].pipeline is False


def _patch_counts(monkeypatch, rows_by_start, *, dimension="service"):
    def fake(start, end, group_by=None, dataset=None, severity=None, limit=None):
        assert group_by == dimension
        assert limit == ranking.CANDIDATE_GROUPS
        record_query(f"SELECT /* {dataset} {start} */ 1")
        return QueryResult(
            list(rows_by_start[start]), stats=QueryStats(dataset=dataset)
        )

    monkeypatch.setattr(specs, "count_by_field", fake)


def test_the_tool_returns_a_ranked_envelope_with_every_query(monkeypatch):
    rows_by_start = {
        BASELINE_START: _rows(
            "service", ("a", "ERROR", 10), ("b", "ERROR", 30)
        ),
        RECENT_START: _rows("service", ("a", "ERROR", 40), ("b", "ERROR", 10)),
    }
    _patch_counts(monkeypatch, rows_by_start)
    result = _call(
        "rank_priorities_tool",
        {"datasets": ["openstack"], "group_by": "service", "limit": 1, **VALID},
    )
    assert not result.is_error
    envelope = result.structured_content
    assert envelope["status"] == "success" and envelope["kind"] == "derived"
    assert envelope["returned"] == 1
    # Two values were ranked; only one row was returned.
    assert envelope["total_matches"] == 2 and envelope["truncated"] is True
    assert envelope["data"][0]["dataset"] == "openstack"
    assert envelope["filters"]["group_by"] == "service"
    # Every executed query is reported.
    assert envelope["query"].count("SELECT") == 2
    assert BASELINE_START in envelope["query"] and RECENT_START in envelope["query"]
    notes = envelope["query_stats"]["notes"]
    assert any(priority.FORMULA in note for note in notes)


def test_a_refusal_from_an_inner_call_reaches_the_client(monkeypatch):
    def refused(*args, **kwargs):
        return QueryResult(
            [],
            stats=QueryStats(dataset="otel_s3"),
            refusal=Refusal(
                reason="the window is wider than 300 s",
                suggestion="narrow it to 300 s",
                code="window_too_wide",
            ),
        )

    monkeypatch.setattr(specs, "count_by_field", refused)
    result = _call(
        "rank_priorities_tool",
        {"datasets": ["otel_s3"], "group_by": "service", **VALID},
    )
    assert not result.is_error
    body = result.structured_content
    assert body["status"] == "refused"
    assert body["suggestion"] == "narrow it to 300 s"


def test_team_mode_calls_count_by_team_once_per_window(monkeypatch):
    calls: list[str] = []

    def fake_team(datasets=None, start_time=None, end_time=None, category=None):
        calls.append(start_time)
        record_query(f"SELECT /* {start_time} */ 1")
        rows = (
            [{"dataset": "openstack", "team": "Compute", "severity": "ERROR", "count": 10}]
            if start_time == BASELINE_START
            else [{"dataset": "openstack", "team": "Compute", "severity": "ERROR", "count": 20}]
        )
        return QueryResult(rows, stats=QueryStats(dataset="openstack"))

    monkeypatch.setattr(specs, "count_by_team", fake_team)

    def no_field(*args, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("service counts used in team mode")

    monkeypatch.setattr(specs, "count_by_field", no_field)
    result = _call(
        "rank_priorities_tool",
        {"datasets": ["openstack"], "group_by": "team", **VALID},
    )
    assert calls == [BASELINE_START, RECENT_START]
    envelope = result.structured_content
    assert envelope["data"][0]["team"] == "Compute"
    assert envelope["data"][0]["non_info_rows"] == 30
