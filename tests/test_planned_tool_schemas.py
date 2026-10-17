"""Offline checks for the planned T2/T3 tool schemas (docs/tool_requests.md).

Only the argument contracts exist; the tools are stubs and are not registered
with any transport yet.
"""

import pytest
from pydantic import ValidationError

from backend.pipeline.tools.envelope import TOOL_KINDS
from backend.pipeline.tools.registry import LOCAL_DB_TOOLS
from backend.pipeline.tools.schemas import CountByTeam, GetNearbyEvents


def test_nearby_events_defaults_and_bounds():
    args = GetNearbyEvents(line_id=7)
    assert (args.before, args.after) == (5, 5)
    assert args.same_entity is False and args.same_request is False
    for bad in ({"line_id": 0}, {"line_id": 7, "before": 51},
                {"line_id": 7, "after": -1}, {"line_id": 7, "dataset": "t"}):
        with pytest.raises(ValidationError):
            GetNearbyEvents(**bad)


def test_count_by_team_window_is_optional_but_paired_and_ordered():
    assert CountByTeam().start_time is None
    CountByTeam(start_time="2017-05-16 00:00:00", end_time="2017-05-17 00:00:00")
    with pytest.raises(ValidationError):
        CountByTeam(start_time="2017-05-16 00:00:00")
    with pytest.raises(ValidationError):
        CountByTeam(start_time="2017-05-17 00:00:00", end_time="2017-05-16 00:00:00")


def test_count_by_team_accepts_only_local_datasets():
    assert CountByTeam(datasets=["openstack", "openstack"]).datasets == ["openstack"]
    with pytest.raises(ValidationError):
        CountByTeam(datasets=["otel_s3"])


def test_planned_tools_have_kinds_but_are_not_registered():
    assert TOOL_KINDS["get_nearby_events_tool"] == "context"
    assert TOOL_KINDS["count_by_team_tool"] == "aggregate"
    names = {tool.name for tool in LOCAL_DB_TOOLS}
    assert not names & {"get_nearby_events_tool", "count_by_team_tool"}
