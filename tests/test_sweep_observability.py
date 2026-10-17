"""Observability for the chunk sweep.

The sweep is the one operation whose reasoning would otherwise be invisible. Its
entire cost -- up to 1,624 chunks, four stages, minutes of wall clock -- happens
inside a *single* tool call, because per-effort budgets cap all tool calls at
between 6 and 22. Without reporting from inside that call, the most expensive
thing the system does would also be its most opaque: a long silence, then a
result with no account of how it was reached.

Three things are asserted here:

* events are emitted **while** the sweep runs, per stage, with counts;
* the **evaluation** is visible -- each chunk's score, the three weighted
  signals behind it, the words that matched, and the verdict;
* the trace survives on the result, because the stream is gone once the run
  ends and these decisions are worth auditing afterwards.
"""

from __future__ import annotations

import pytest

from backend.db import chunks as chunk_mod
from backend.db import profile as profile_mod
from backend.db import pruning
from backend.db.datasets import resolve
from backend.db.promotion import ChunkDigest
from backend.db.stats import QueryResult, QueryStats
from backend.pipeline import sweep as sweep_mod
from backend.pipeline.tools.executor import ProgressReporter

URL = "https://public-pme.s3.eu-west-3.amazonaws.com/text_bench/part_000.parquet"
QUESTION = "charge card failing"


@pytest.fixture(autouse=True)
def seeded():
    pruning.clear_cache()
    pruning.seed_cache(URL, pruning.synthetic_row_groups(URL, 300))
    chunk_mod.clear_plan_cache()
    yield
    pruning.clear_cache()
    chunk_mod.clear_plan_cache()


@pytest.fixture(autouse=True)
def stub_stages(monkeypatch):
    def fake_profile(start, end, dataset=None, bucket="second", by_service=False):
        plan = chunk_mod.plan_chunks(resolve("otel_s3"))
        rows = [
            {"bucket": chunk.ts_lo, "severity": "error", "count": 9_000}
            for chunk in plan[:5]
        ]
        return QueryResult(
            rows, stats=QueryStats(dataset="otel_s3", row_count=len(rows))
        )

    def fake_sample(chunk, dataset=None, windows=8, **kwargs):
        rows = [
            {
                "ts": chunk.ts_lo,
                "severity": "error",
                "service": "payment",
                "message": "failed to charge card: could not charge",
            }
        ] * 8
        return QueryResult(
            rows, stats=QueryStats(dataset="otel_s3", row_count=len(rows))
        )

    def fake_promote(chunk, **kwargs):
        return ChunkDigest(
            chunk_id=chunk.chunk_id,
            rows_indexed=90_000,
            rows_scanned=chunk.num_rows,
            ts_lo=chunk.ts_lo,
            ts_hi=chunk.ts_hi,
            severity_counts={"error": 90_000},
            service_counts={"payment": 90_000},
            top_templates=(("failed to charge card: <*>", 40_000),),
            exemplars=({"message": "failed to charge card", "ts": "t"},),
            triage_score=kwargs.get("triage_score"),
            build_seconds=12.3,
            bytes_used=14_000_000,
        )

    monkeypatch.setattr(sweep_mod.profile_mod, "profile_buckets", fake_profile)
    monkeypatch.setattr(sweep_mod.profile_mod, "sample_chunk", fake_sample)
    monkeypatch.setattr(sweep_mod, "promote_chunk", fake_promote)


def run(**budget_kwargs):
    budget = sweep_mod.SweepBudget(
        max_promotions=budget_kwargs.pop("max_promotions", 1),
        max_sampled_chunks=budget_kwargs.pop("max_sampled_chunks", 2),
        **budget_kwargs,
    )
    return sweep_mod.sweep(QUESTION, budget=budget)


# --- Events are emitted while the sweep runs --------------------------------
def test_every_stage_reports_itself():
    seen = [event.stage for event in run().events]
    for stage in ("map", "profile", "sample", "decide", "promote", "done"):
        assert stage in seen, stage


def test_events_arrive_during_the_run_not_only_at_the_end():
    """A result-only report would leave minutes of silence."""
    live: list[str] = []
    sweep_mod.sweep(
        QUESTION,
        budget=sweep_mod.SweepBudget(max_promotions=1, max_sampled_chunks=2),
        progress=lambda event: live.append(event.stage),
    )
    assert live[0] == "map"
    assert live[-1] == "done"
    assert live.count("profile") > 1


def test_profiling_reports_running_counts():
    """Progress has to be legible as progress: 120 of 300, then 240 of 300."""
    profiles = [e for e in run().events if e.stage == "profile"]
    seen = [e.detail["chunks_profiled"] for e in profiles]

    assert seen == sorted(seen)
    assert seen[-1] == profiles[-1].detail["chunks_total"]


def test_each_stage_says_whether_it_read_message_text():
    """The distinction that explains the cost difference between stages."""
    events = run().events
    profile_event = next(e for e in events if e.stage == "profile")
    promote_event = next(e for e in events if e.stage == "promote")

    assert profile_event.detail["reads_message_text"] is False
    assert promote_event.detail["reads_message_text"] is True


def test_the_map_stage_states_what_is_addressable():
    event = next(e for e in run().events if e.stage == "map")
    assert event.detail["chunks"] == 300
    assert event.detail["rows_addressable"] > 100_000_000


def test_the_final_event_summarises_the_whole_sweep():
    event = next(e for e in run().events if e.stage == "done")
    assert event.detail["chunks_profiled"] == 300
    assert event.detail["chunks_promoted"] == 1
    assert "300" in event.message


# --- The evaluation is visible ----------------------------------------------
def test_sampling_exposes_the_score_and_its_components():
    """"How are they evaluated" means the weighted signals, not just a verdict.

    A score with no components is unauditable: there would be no way to tell a
    chunk that ranked highly on error density from one that matched the
    question's words.
    """
    event = next(e for e in run().events if e.stage == "sample")
    for key in (
        "score",
        "verdict",
        "reason",
        "term_hit_rate",
        "error_density",
        "service_overlap",
        "matched_terms",
    ):
        assert key in event.detail, key


def test_the_matched_words_are_named():
    """So a surprising verdict can be traced to the words that caused it."""
    event = next(e for e in run().events if e.stage == "sample")
    assert event.detail["matched_terms"]
    assert "charge" in event.detail["matched_terms"]


def test_a_promotion_decision_states_its_reason_and_price_first():
    """The decision is reported before the work, not after.

    Eleven minutes is long enough that a reader needs to know why it started.
    """
    events = run().events
    decide_at = next(i for i, e in enumerate(events) if e.stage == "decide")
    promote_at = next(i for i, e in enumerate(events) if e.stage == "promote")

    assert decide_at < promote_at
    decision = events[decide_at]
    assert decision.detail["score"] > 0
    assert decision.detail["reason"]
    assert decision.detail["estimated_seconds"] > 0


def test_the_promotion_result_reports_what_it_produced():
    event = next(e for e in run().events if e.stage == "promote")
    assert event.detail["rows_indexed"] == 90_000
    assert event.detail["rows_scanned"] > event.detail["rows_indexed"]
    assert event.detail["top_template"]


def test_a_sweep_that_reads_nothing_says_so_as_a_decision():
    """Silence here would read as "nothing was found" rather than "nothing was
    looked at closely"."""
    result = run(max_promotions=0)
    decisions = [e for e in result.events if e.stage == "decide"]
    assert decisions
    assert decisions[-1].detail["promotions_allowed"] == 0
    assert not any(e.stage == "promote" for e in result.events)


# --- The trace survives the run --------------------------------------------
def test_the_trace_is_kept_on_the_result():
    result = run()
    assert result.events
    assert len(result.as_dict()["trace"]) == len(result.events)


def test_the_trace_is_recorded_without_a_listener():
    """Auditable after the fact, not only while streaming."""
    result = sweep_mod.sweep(
        QUESTION, budget=sweep_mod.SweepBudget(max_promotions=1)
    )
    assert result.events


def test_the_serialized_trace_leaks_no_urls():
    assert "https://" not in str(run().as_dict()["trace"])


# --- The reporter adapter ---------------------------------------------------
def test_the_reporter_forwards_stage_and_detail():
    written: list[dict] = []
    reporter = ProgressReporter(written.append, "sweep otel_s3")
    reporter("profiled 120 chunks", stage="profile", detail={"chunks": 120})

    assert written[0]["type"] == "tool_progress"
    assert written[0]["status"] == "working"
    assert written[0]["stage"] == "profile"
    assert written[0]["detail"] == {"chunks": 120}


def test_the_reporter_keeps_its_own_events_for_the_result():
    reporter = ProgressReporter(lambda _p: None, "sweep otel_s3")
    reporter("a", stage="map")
    reporter("b", stage="done")
    assert [e["stage"] for e in reporter.events] == ["map", "done"]


def test_working_status_is_distinct_from_completed_and_failed():
    """The UI keys off status, and mislabelling progress as failure would make
    a healthy sweep look broken."""
    reporter = ProgressReporter(lambda _p: None, "op")
    reporter("mid-flight", stage="profile")
    assert reporter.events[0]["status"] == "working"


def test_the_sweep_tool_relays_events_to_the_reporter():
    from backend.pipeline.tools.specs import _relay

    written: list[dict] = []
    reporter = ProgressReporter(written.append, "sweep otel_s3")
    relay = _relay(reporter)
    relay(sweep_mod.SweepEvent(stage="sample", message="scored a chunk", detail={"score": 0.8}))

    assert written[0]["stage"] == "sample"
    assert written[0]["message"] == "scored a chunk"
    assert written[0]["detail"]["score"] == 0.8


# --- Display ---------------------------------------------------------------
def test_the_trace_renders_as_rows():
    from ui.rendering import sweep_trace_rows

    result = run()
    records = [
        {
            "id": "R1",
            "tool_name": "sweep_chunks_tool",
            "progress_trace": [e.as_dict() for e in result.events],
        }
    ]
    rows = sweep_trace_rows(records)

    assert len(rows) == len(result.events)
    assert rows[0]["Result"] == "R1"
    assert any(row["Read text"] == "yes" for row in rows)
    assert any(row["Score"] != "-" for row in rows)


def test_results_without_a_trace_render_nothing():
    from ui.rendering import sweep_trace_rows

    assert sweep_trace_rows([{"id": "R1", "tool_name": "count_by_category_tool"}]) == []
    assert sweep_trace_rows(None) == []


def test_the_trace_panel_is_collapsed_and_labelled():
    """Supporting detail, not the headline: the answer's coverage line is."""
    from ui.rendering import render_sweep_trace

    class FakePanel:
        def __init__(self):
            self.frames = []

        def dataframe(self, frame, **kwargs):
            self.frames.append(frame)

    class FakeContainer:
        def __init__(self):
            self.panel = FakePanel()
            self.label = None
            self.expanded = None

        def expander(self, label, expanded=False):
            self.label = label
            self.expanded = expanded
            return self.panel

    container = FakeContainer()
    result = run()
    render_sweep_trace(
        container,
        [{"id": "R1", "progress_trace": [e.as_dict() for e in result.events]}],
    )

    assert container.expanded is False
    assert "swept" in container.label
    assert container.panel.frames


def test_the_record_collector_preserves_the_trace():
    """The display contract has to carry it or the panel never sees it."""
    import inspect

    from backend.pipeline.tools import results as results_mod

    source = inspect.getsource(results_mod.collect_tool_results)
    assert "progress_trace" in source
