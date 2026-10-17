"""Sweep orchestration: budgets are hard stops, and coverage is stated.

The sweep's reason for existing is that a user may legitimately insist on
evidence from a whole billion-row corpus, which no single query can provide.
What makes it affordable is that the four stages differ in cost by orders of
magnitude -- profiling reads no message text and scales to everything, promotion
reads text and costs ~11 minutes a chunk.

Two properties matter more than the rest, and both are easy to lose:

* **the loop must stay inside one tool call.** Per-effort budgets cap all tool
  calls at 4-20, to-do writes included, so 1,624 chunks cannot be 1,624 calls.
* **coverage must be reported.** A bounded sweep produces false negatives by
  construction; an answer that does not say what it examined invites the reader
  to assume it examined everything.

Profiling, sampling and promotion are stubbed. The real operations need the
remote corpus and, for promotion, eleven minutes a chunk.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from backend.db import chunks as chunk_mod  # noqa: E402
from backend.db import profile as profile_mod  # noqa: E402
from backend.db import pruning  # noqa: E402
from backend.db.datasets import resolve  # noqa: E402
from backend.db.errors import QueryRefused  # noqa: E402
from backend.db.promotion import ChunkDigest  # noqa: E402
from backend.db.stats import QueryResult, QueryStats  # noqa: E402
from backend.pipeline import sweep as sweep_mod  # noqa: E402

URL = "https://public-pme.s3.eu-west-3.amazonaws.com/text_bench/part_000.parquet"


@pytest.fixture(autouse=True)
def seeded():
    """A synthetic 1,624-chunk map, so no footer is ever fetched."""
    pruning.clear_cache()
    pruning.seed_cache(URL, pruning.synthetic_row_groups(URL, 1624))
    chunk_mod.clear_plan_cache()
    yield
    pruning.clear_cache()
    chunk_mod.clear_plan_cache()


@pytest.fixture
def stub_stages(monkeypatch):
    """Replace the three costly stages with counted, instant substitutes."""
    calls = {"profile": 0, "sample": 0, "promote": 0}

    def fake_profile(start, end, dataset=None, bucket="second", by_service=False):
        calls["profile"] += 1
        plan = chunk_mod.plan_chunks(resolve("otel_s3"))
        rows = [
            {
                "bucket": chunk.ts_lo,
                "severity": "error",
                "count": 5_000 + index,
            }
            for index, chunk in enumerate(plan[:4])
        ]
        return QueryResult(rows, stats=QueryStats(dataset="otel_s3", row_count=len(rows)))

    def fake_sample(chunk, dataset=None, windows=8, **kwargs):
        calls["sample"] += 1
        rows = [
            {
                "ts": chunk.ts_lo,
                "severity": "error",
                "service": "payment",
                "message": "failed to charge card: could not charge",
            }
        ] * 10
        return QueryResult(rows, stats=QueryStats(dataset="otel_s3", row_count=len(rows)))

    def fake_promote(chunk, **kwargs):
        calls["promote"] += 1
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
        )

    monkeypatch.setattr(profile_mod, "profile_buckets", fake_profile)
    monkeypatch.setattr(sweep_mod.profile_mod, "profile_buckets", fake_profile)
    monkeypatch.setattr(sweep_mod.profile_mod, "sample_chunk", fake_sample)
    monkeypatch.setattr(sweep_mod, "promote_chunk", fake_promote)
    return calls


# --- The constraint that shapes everything ----------------------------------
def test_sixteen_hundred_chunks_cost_a_handful_of_queries(stub_stages):
    """The sweep must not be one query per chunk.

    All 1,624 chunks are profiled by batched aggregates. If this ever became
    per-chunk, a sweep would exhaust any tool-call budget before finishing.
    """
    result = sweep_mod.sweep("why are payments failing", budget=sweep_mod.SweepBudget())

    assert result.progress.chunks_profiled == 1624
    assert stub_stages["profile"] <= 20


def test_profiling_batches_are_bounded(stub_stages):
    expected = -(-1624 // sweep_mod.PROFILE_BATCH_CHUNKS)
    sweep_mod.sweep("payments", budget=sweep_mod.SweepBudget())
    assert stub_stages["profile"] == expected


# --- Budgets are hard stops -------------------------------------------------
def test_the_profile_budget_limits_how_much_is_examined(stub_stages):
    budget = sweep_mod.SweepBudget(max_profiled_chunks=240)
    result = sweep_mod.sweep("payments", budget=budget)
    assert result.progress.chunks_profiled == 240
    assert result.progress.chunks_profiled < result.progress.chunks_total


def test_the_sample_budget_limits_text_reads(stub_stages):
    budget = sweep_mod.SweepBudget(max_sampled_chunks=3)
    result = sweep_mod.sweep("payments", budget=budget)
    assert result.progress.chunks_sampled <= 3
    assert stub_stages["sample"] <= 3


def test_no_promotion_happens_at_zero_allowance(stub_stages):
    """Promotion is gated on effort; most levels do not permit it."""
    result = sweep_mod.sweep("payments", budget=sweep_mod.SweepBudget(max_promotions=0))
    assert stub_stages["promote"] == 0
    assert result.digests == ()


def test_promotions_are_capped(stub_stages):
    """Uses a question whose words appear in the sampled text.

    With a question that does not match, scoring lands in the ambiguous band
    and nothing is promoted -- which would make this assertion pass for the
    wrong reason.
    """
    budget = sweep_mod.SweepBudget(max_promotions=2)
    result = sweep_mod.sweep("charge card failing", budget=budget)

    assert stub_stages["promote"] == 2
    assert len(result.digests) == 2


def test_a_chunk_is_promoted_only_on_evidence_from_its_text(stub_stages):
    """Term hits come from the sample, so a question sharing no word with the
    messages leaves chunks ambiguous rather than promoting them.

    Matching is literal: 'payments' does not match 'payment'. Semantic reach is
    the template-embedding search's job, not triage's.
    """
    unmatched = sweep_mod.sweep(
        "payments", budget=sweep_mod.SweepBudget(max_promotions=2)
    )
    assert unmatched.digests == ()

    matched = sweep_mod.sweep(
        "charge card failing", budget=sweep_mod.SweepBudget(max_promotions=2)
    )
    assert matched.digests


def test_the_time_budget_stops_the_sweep(stub_stages):
    """A zero budget must stop immediately rather than run one of everything."""
    result = sweep_mod.sweep("payments", budget=sweep_mod.SweepBudget(max_seconds=0))
    assert result.progress.stopped_because
    assert "time budget" in result.progress.stopped_because


def test_the_scan_budget_stops_the_sweep(stub_stages):
    budget = sweep_mod.SweepBudget(max_rows_scanned=1_000_000)
    result = sweep_mod.sweep("payments", budget=budget)
    assert result.progress.stopped_because
    assert "scan budget" in result.progress.stopped_because


def test_a_stopped_sweep_still_returns_what_it_found(stub_stages):
    """Partial results with a stated stop reason beat an exception."""
    result = sweep_mod.sweep(
        "payments", budget=sweep_mod.SweepBudget(max_rows_scanned=5_000_000)
    )
    assert result.progress.chunks_profiled > 0
    assert result.coverage
    assert result.progress.stopped_because


# --- Coverage honesty -------------------------------------------------------
def test_coverage_reports_each_stage_separately(stub_stages):
    """Profiled, sampled and promoted are different depths of examination and
    must not be conflated: a profiled chunk was counted, not read."""
    result = sweep_mod.sweep(
        "charge card failing", budget=sweep_mod.SweepBudget(max_promotions=1)
    )
    coverage = result.coverage

    for key in (
        "chunks_total",
        "chunks_profiled",
        "chunks_sampled",
        "chunks_promoted",
        "fraction_profiled",
    ):
        assert key in coverage


def test_coverage_states_the_limitation(stub_stages):
    result = sweep_mod.sweep("payments", budget=sweep_mod.SweepBudget())
    assert "limitation" in result.coverage


def test_a_sweep_without_promotions_says_no_text_was_read(stub_stages):
    """Otherwise a ranked list looks like evidence when it is only a ranking."""
    result = sweep_mod.sweep("payments", budget=sweep_mod.SweepBudget(max_promotions=0))
    assert any("no message text" in note for note in result.notes)


def test_the_result_serializes_without_urls(stub_stages):
    result = sweep_mod.sweep(
        "charge card failing", budget=sweep_mod.SweepBudget(max_promotions=1)
    )
    assert result.digests
    assert "https://" not in str(result.as_dict())


def test_the_result_carries_merged_query_stats(stub_stages):
    """Cost reporting is computed from these, off-model."""
    result = sweep_mod.sweep("payments", budget=sweep_mod.SweepBudget())
    assert result.stats is not None
    assert result.as_dict()["query_stats"] is not None


# --- Resume -----------------------------------------------------------------
def test_the_cursor_advances_monotonically(stub_stages):
    result = sweep_mod.sweep("payments", budget=sweep_mod.SweepBudget())
    assert result.cursor == result.progress.chunks_profiled


def test_resuming_starts_where_the_last_sweep_stopped(stub_stages):
    first = sweep_mod.sweep(
        "payments", budget=sweep_mod.SweepBudget(max_profiled_chunks=240)
    )
    second = sweep_mod.sweep(
        "payments",
        budget=sweep_mod.SweepBudget(max_profiled_chunks=240),
        start_at=first.cursor,
    )
    assert second.cursor == first.cursor + second.progress.chunks_profiled
    assert second.cursor > first.cursor


def test_resuming_past_the_end_is_harmless(stub_stages):
    result = sweep_mod.sweep("payments", start_at=10_000)
    assert result.progress.chunks_profiled == 0
    assert result.coverage


# --- Robustness -------------------------------------------------------------
def test_one_failed_promotion_does_not_end_the_sweep(stub_stages, monkeypatch):
    """A chunk that cannot be promoted is a note, not an exception: the other
    chunks' digests are still worth having."""
    def exploding(chunk, **kwargs):
        raise RuntimeError("index build failed")

    monkeypatch.setattr(sweep_mod, "promote_chunk", exploding)
    result = sweep_mod.sweep(
        "charge card failing", budget=sweep_mod.SweepBudget(max_promotions=2)
    )

    assert result.digests == ()
    assert any("failed" in note for note in result.notes)
    assert result.coverage


def test_a_refused_profile_is_noted_and_skipped(stub_stages, monkeypatch):
    from backend.db.errors import Refusal

    def refused(*args, **kwargs):
        result = QueryResult([], stats=None)
        result.refusal = Refusal(
            reason="window too wide", suggestion="narrow it", code="window_too_wide"
        )
        return result

    monkeypatch.setattr(sweep_mod.profile_mod, "profile_buckets", refused)
    result = sweep_mod.sweep("payments", budget=sweep_mod.SweepBudget())

    assert any("refused" in note for note in result.notes)
    assert result.cursor > 0


def test_progress_is_reported_per_stage(stub_stages):
    """A sweep can run for minutes, so the UI needs ticks while it does.

    The callback takes one structured event rather than a stage and a counter:
    the detail payload is what makes the evaluation auditable, and it has
    nowhere to live in a two-argument signature. Covered in depth by
    ``test_sweep_observability.py``.
    """
    seen: list[str] = []
    sweep_mod.sweep(
        "charge card failing",
        budget=sweep_mod.SweepBudget(max_promotions=1),
        progress=lambda event: seen.append(event.stage),
    )
    assert "profile" in seen
    assert "sample" in seen
    assert "promote" in seen


# --- Gating -----------------------------------------------------------------
def test_a_local_corpus_cannot_be_swept():
    """The sweep addresses a remote corpus by row group; BGL is local."""
    with pytest.raises(QueryRefused) as excinfo:
        sweep_mod.sweep("anything", dataset="bgl")
    assert excinfo.value.refusal.code == "not_chunkable"


def test_an_estimate_prices_promotion_as_the_dominant_cost():
    """What Task 17 asks a user to confirm."""
    estimate = sweep_mod.estimate_sweep(budget=sweep_mod.SweepBudget(max_promotions=2))
    assert estimate["estimated_promote_seconds"] > estimate["estimated_profile_seconds"]
    assert estimate["chunks_available"] == 1624
    assert estimate["rows_addressable"] > 900_000_000


def test_an_estimate_is_available_without_running_anything(stub_stages):
    estimate = sweep_mod.estimate_sweep()
    assert stub_stages["profile"] == 0
    assert estimate["estimated_seconds"] >= 0


def test_investigating_one_chunk_states_its_narrow_coverage(stub_stages):
    """A single-chunk read must not read as a corpus-wide finding."""
    plan = chunk_mod.plan_chunks(resolve("otel_s3"))
    result = sweep_mod.investigate_chunk(plan[5].chunk_id, question="payments")

    assert result["chunk_id"] == plan[5].chunk_id
    assert "unexamined" in result["coverage"]
