"""Offline checks for the planned T3 tool schema (docs/tool_requests.md).

Only the argument contract exists; the tool is a stub and is not registered
with any transport yet. T2 (``get_nearby_events_tool``) is implemented and
tested in ``test_nearby_events.py``.
"""

import pytest
from pydantic import ValidationError

from backend.pipeline.tools.envelope import TOOL_KINDS
from backend.pipeline.tools.mcp_tools import MCP_DB_TOOLS
from backend.pipeline.tools.registry import LOCAL_DB_TOOLS
from backend.pipeline.tools.schemas import CountByTeam


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


def test_planned_tool_has_a_kind_but_is_not_registered():
    assert TOOL_KINDS["count_by_team_tool"] == "aggregate"
    for tools in (LOCAL_DB_TOOLS, MCP_DB_TOOLS):
        assert "count_by_team_tool" not in {tool.name for tool in tools}
