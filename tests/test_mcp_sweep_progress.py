"""M6: the sweep reports progress to the MCP client and the server caps its cost."""

from __future__ import annotations

import asyncio

import pytest
from fastmcp import Client

from backend.db.stats import QueryResult
from backend.mcp_server import config, server
from backend.mcp_server.tools import SWEEP_TAG
from backend.pipeline.sweep import SweepEvent
from backend.pipeline.tools import specs

SWEEP_ARGS = {
    "dataset": "otel_s3",
    "question": "payment failed",
    "confirm_cost": True,
    "max_promotions": 1,
}
STAGES = ["map", "profile", "sample", "decide", "promote", "done"]


def _fake_sweep(*_a, progress=None, **_k):
    for stage in STAGES:
        progress(SweepEvent(stage, f"{stage} happened", {"n": 1}))
    return "result"


@pytest.fixture
def fake_sweep(monkeypatch):
    monkeypatch.setattr(specs, "sweep", _fake_sweep)
    monkeypatch.setattr(specs, "_sweep_rows", lambda r: QueryResult([{"ok": True}], stats=None))


def _call(name, arguments, **client_kwargs):
    async def go():
        async with Client(server.mcp, **client_kwargs) as client:
            return await client.call_tool(name, arguments, raise_on_error=False)

    return asyncio.run(go())


def test_sweep_stages_arrive_as_ordered_progress_notifications(fake_sweep):
    seen = []

    async def on_progress(progress, total, message):
        seen.append((progress, total, message))

    result = _call("sweep_chunks_tool", SWEEP_ARGS, progress_handler=on_progress)
    assert not result.is_error
    assert [m for _, _, m in seen] == [f"[{s}] {s} happened" for s in STAGES]
    counters = [p for p, _, _ in seen]
    assert counters == sorted(counters) and len(set(counters)) == len(counters)


def test_investigate_chunk_reports_start_and_finish(monkeypatch):
    class Digest:
        chunk_id = "part_000:1-1"
        rows_indexed = 5
        rows_scanned = 50
        build_seconds = 1.0
        top_templates = ()

        def as_dict(self):
            return {}

    import backend.pipeline.sweep as sweep_mod

    monkeypatch.setattr(sweep_mod, "_require_sweepable", lambda d: "otel_s3")
    monkeypatch.setattr(sweep_mod.chunk_mod, "find_chunk", lambda ds, cid: object())
    monkeypatch.setattr(sweep_mod, "promote_chunk", lambda *a, **k: Digest())
    seen = []

    async def on_progress(progress, total, message):
        seen.append(message)

    result = _call(
        "investigate_chunk_tool",
        {"dataset": "otel_s3", "chunk_id": "part_000:1-1", "question": "q"},
        progress_handler=on_progress,
    )
    assert not result.is_error, result.content
    assert len(seen) == 2
    assert seen[0].startswith("[promote] Reading") and seen[1].startswith("[promote] Read ")


def test_a_failing_notification_does_not_stop_the_sweep(fake_sweep):
    async def broken(progress, total, message):
        raise RuntimeError("client went away")

    result = _call("sweep_chunks_tool", SWEEP_ARGS, progress_handler=broken)
    assert not result.is_error


def test_no_progress_handler_still_completes(fake_sweep):
    assert not _call("sweep_chunks_tool", SWEEP_ARGS).is_error


# --- the promotion cap ---------------------------------------------------------------
def _structured(result):
    return result.structured_content


def test_a_call_over_the_cap_is_refused_with_a_suggestion(monkeypatch, fake_sweep):
    monkeypatch.setattr(config, "MCP_MAX_PROMOTIONS", 1)
    result = _call("sweep_chunks_tool", {**SWEEP_ARGS, "max_promotions": 3})
    assert not result.is_error
    body = _structured(result)
    assert body["status"] == "refused"
    text = str(body)
    assert "limit of 1" in text and "max_promotions=1" in text


def test_a_call_at_the_cap_runs(monkeypatch, fake_sweep):
    monkeypatch.setattr(config, "MCP_MAX_PROMOTIONS", 2)
    body = _structured(_call("sweep_chunks_tool", {**SWEEP_ARGS, "max_promotions": 2}))
    assert body["status"] != "refused"


def test_cap_zero_refuses_reading_a_chunk_but_not_a_free_sweep(monkeypatch, fake_sweep):
    monkeypatch.setattr(config, "MCP_MAX_PROMOTIONS", 0)
    read = _structured(
        _call(
            "investigate_chunk_tool",
            {"dataset": "otel_s3", "chunk_id": "part_000:1-1", "question": "q"},
        )
    )
    assert read["status"] == "refused" and "sweep_chunks_tool" in str(read)
    free = _structured(_call("sweep_chunks_tool", {**SWEEP_ARGS, "max_promotions": 0}))
    assert free["status"] != "refused"


def test_the_default_cap_changes_nothing():
    assert config.MCP_MAX_PROMOTIONS == 4


# --- hiding the sweep ----------------------------------------------------------------
def test_sweep_tools_are_tagged_and_visible_by_default():
    async def names():
        async with Client(server.mcp) as client:
            return {t.name for t in await client.list_tools()}

    visible = asyncio.run(names())
    assert {"sweep_chunks_tool", "investigate_chunk_tool"} <= visible
    tagged = {t.name for t in asyncio.run(server.mcp.list_tools()) if SWEEP_TAG in t.tags} \
        if hasattr(server.mcp, "list_tools") else None
    if tagged is not None:
        assert tagged == {"sweep_chunks_tool", "investigate_chunk_tool"}


def test_the_sweep_can_be_hidden_by_tag():
    async def go():
        server.mcp.disable(tags={SWEEP_TAG})
        try:
            async with Client(server.mcp) as client:
                return {t.name for t in await client.list_tools()}
        finally:
            server.mcp.enable(tags={SWEEP_TAG})

    names = asyncio.run(go())
    assert "sweep_chunks_tool" not in names and "investigate_chunk_tool" not in names
    assert "search_logs_tool" in names
