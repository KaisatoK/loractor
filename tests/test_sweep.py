"""Chunk map, coarse profiling, stratified sampling, and triage scoring.

Offline: the chunk map is built from seeded footer metadata, SQL is rendered by
``tests/sql_render.py``, and scoring is pure arithmetic over fixture samples.

Measured facts these tests encode (part_000):

* a row group is ~614,400 rows spanning ~1.1 s, which is **larger** than the
  promotion row ceiling -- so every chunk is exactly one row group
* adjacent row groups **overlap in time**, which is why chunks tile on successor
  boundaries rather than on their own max timestamp
"""

from __future__ import annotations

import pytest
from sql_render import normalized

from backend.db import chunks as chunk_mod
from backend.db import profile as profile_mod
from backend.db import pruning
from backend.db.budget import CHUNK_ROWS, PROBLEM_SEVERITIES
from backend.db.datasets import resolve
from backend.db.errors import QueryRefused
from backend.db.profile import ChunkProfile, attach_profiles, micro_windows
from backend.pipeline import triage

URL = "https://example.invalid/part_000.parquet"


@pytest.fixture(autouse=True)
def seeded():
    """Footer metadata shaped like the real corpus, for one 1,624-group file."""
    pruning.clear_cache()
    chunk_mod.clear_plan_cache()
    dataset = resolve("otel_s3")
    for url in dataset.sources:
        pruning.seed_cache(url, pruning.synthetic_row_groups(url, 1624))
    yield
    pruning.clear_cache()
    chunk_mod.clear_plan_cache()


@pytest.fixture
def otel():
    return resolve("otel_s3")


@pytest.fixture
def plan(otel):
    return chunk_mod.plan_chunks(otel)


@pytest.fixture
def fixture_rows(otel):
    """Total rows the seeded footers describe.

    The synthetic fixture uses uniform row groups, while the real file's groups
    vary (614,400 / 622,592, and a short final group). The invariant under test
    is "the map covers every row the file has", so it is asserted against the
    fixture's own total rather than the real corpus figure.
    """
    return sum(
        g.num_rows
        for url in otel.sources
        for g in pruning.load_row_groups(url, "Timestamp")
    )


# --- Chunk map --------------------------------------------------------------
def test_the_map_covers_every_row_with_no_gaps(plan, fixture_rows):
    assert sum(c.num_rows for c in plan) == fixture_rows
    for earlier, later in zip(plan, plan[1:]):
        assert earlier.last_row_group + 1 == later.first_row_group


def test_chunks_tile_exactly_in_time(plan):
    """No overlap and no hole: each chunk ends where the next begins.

    Required for correctness, because adjacent row groups overlap in time on
    this corpus -- a chunk whose end came from its own max timestamp would both
    overlap its successor and risk dropping boundary rows.
    """
    for earlier, later in zip(plan, plan[1:]):
        assert earlier.ts_hi == later.ts_lo


def test_every_chunk_is_one_row_group_because_a_row_group_is_bigger(plan):
    """A row group (~614k rows) exceeds the promotion ceiling, so it is the unit.

    ``num_rows`` is therefore rows *read*; the rows actually indexed are far
    fewer once promotion applies its severity filter.
    """
    assert CHUNK_ROWS < 614_400
    assert all(c.row_groups == 1 for c in plan)
    assert len(plan) == 1_624


def test_chunk_ids_are_stable_and_derived_from_the_row_group_span(plan):
    assert plan[0].chunk_id == "part_000:0-0"
    assert plan[-1].chunk_id == "part_000:1623-1623"
    # Ids, not ordinals, so a resumed sweep means the same thing.
    assert len({c.chunk_id for c in plan}) == len(plan)


def test_the_plan_is_deterministic(otel, plan):
    chunk_mod.clear_plan_cache()
    again = chunk_mod.plan_chunks(otel)
    assert [c.chunk_id for c in again] == [c.chunk_id for c in plan]
    assert [c.ts_lo for c in again] == [c.ts_lo for c in plan]


def test_larger_targets_pack_several_row_groups_per_chunk(otel, fixture_rows):
    chunk_mod.clear_plan_cache()
    packed = chunk_mod.plan_chunks(otel, target_rows=2_000_000)
    assert len(packed) < 1_624
    assert max(c.row_groups for c in packed) >= 3
    # Repacking must not lose or duplicate a row.
    assert sum(c.num_rows for c in packed) == fixture_rows


def test_chunk_predicate_is_a_plain_half_open_time_filter(otel, plan):
    clause, params = chunk_mod.chunk_predicate(otel, plan[0])
    rendered = normalized(clause)
    assert '"Timestamp" >=' in rendered
    assert '< ' in rendered
    assert "::timestamptz" in rendered
    assert params == []


def test_find_chunk_resolves_an_id_and_refuses_an_unknown_one(otel, plan):
    assert chunk_mod.find_chunk(otel, plan[5].chunk_id).chunk_id == plan[5].chunk_id
    with pytest.raises(QueryRefused) as excinfo:
        chunk_mod.find_chunk(otel, "part_000:9999-9999")
    assert excinfo.value.refusal.code == "unknown_chunk"


def test_a_local_corpus_has_no_chunk_map():
    with pytest.raises(QueryRefused) as excinfo:
        chunk_mod.plan_chunks(resolve("openstack"))
    assert excinfo.value.refusal.code == "not_chunkable"


def test_chunks_in_window_scopes_the_sweep(otel, plan):
    selected = chunk_mod.chunks_in_window(otel, plan[10].ts_lo, plan[13].ts_lo)
    assert [c.chunk_id for c in selected] == [c.chunk_id for c in plan[10:13]]


def test_map_summary_reports_the_totals(otel, fixture_rows):
    summary = chunk_mod.map_summary(otel)
    assert summary["chunks"] == 1_624
    assert summary["total_rows"] == fixture_rows
    assert summary["rows_per_chunk"] == CHUNK_ROWS


def test_chunk_dict_exposes_no_urls(plan):
    """Chunk ids reach the model; source URLs are server-side detail."""
    assert "url" not in plan[0].as_dict()
    assert "https" not in repr(plan[0].as_dict())


# --- Stage 1: coarse profile ------------------------------------------------
def test_profile_is_a_single_query_touching_only_cheap_columns():
    """The load-bearing economy: one aggregate profiles every chunk."""
    query, _, stats = profile_mod.profile_buckets.__wrapped__(
        "2025-09-23 00:00:00+00", "2025-09-23 00:00:30+00", dataset="otel_s3"
    )
    rendered = normalized(query)

    assert rendered.count("SELECT") == 1
    assert '"Timestamp"' in rendered
    assert '"SeverityText"' in rendered
    # No message text, and no service unless asked for.
    assert '"Body"' not in rendered
    assert '"ServiceName"' not in rendered
    assert "no message text read" in " ".join(stats.notes)


def test_profile_can_add_the_service_dimension_when_asked():
    query, _, _ = profile_mod.profile_buckets.__wrapped__(
        "2025-09-23 00:00:00+00",
        "2025-09-23 00:00:30+00",
        dataset="otel_s3",
        by_service=True,
    )
    assert '"ServiceName"' in normalized(query)


def test_profile_buckets_by_the_requested_width():
    query, _, _ = profile_mod.profile_buckets.__wrapped__(
        "2025-09-23 00:00:00+00",
        "2025-09-23 00:00:30+00",
        dataset="otel_s3",
        bucket="second",
    )
    assert "date_trunc('second'" in normalized(query)


def test_attach_profiles_maps_buckets_onto_their_chunks(plan):
    rows = [
        {"bucket": plan[0].ts_lo.isoformat(), "severity": "error", "count": 90},
        {"bucket": plan[0].ts_lo.isoformat(), "severity": "info", "count": 10},
        {"bucket": plan[1].ts_lo.isoformat(), "severity": "info", "count": 100},
    ]
    profiles = attach_profiles(plan[:2], rows)

    assert profiles[0].severity_counts == {"error": 90, "info": 10}
    assert profiles[1].severity_counts == {"info": 100}
    assert profiles[0].error_rate(PROBLEM_SEVERITIES) == pytest.approx(0.9)


def test_attach_profiles_drops_buckets_outside_every_chunk(plan):
    rows = [{"bucket": "2030-01-01 00:00:00+00", "severity": "error", "count": 5}]
    profiles = attach_profiles(plan[:2], rows)
    assert all(not p.severity_counts for p in profiles)


# --- Stage 2: stratified sampling -------------------------------------------
def test_micro_windows_span_the_whole_chunk(plan):
    windows = micro_windows(plan[0], 8)
    assert len(windows) == 8
    assert windows[0].start == plan[0].ts_lo.isoformat(sep=" ")
    assert windows[-1].end == plan[0].ts_hi.isoformat(sep=" ")
    # Contiguous, so the sample cannot miss a slice of the chunk.
    for earlier, later in zip(windows, windows[1:]):
        assert earlier.end == later.start


def test_sampling_caps_rows_per_window_not_just_overall(plan):
    """A single overall LIMIT would let the whole sample come from one instant."""
    query, params, stats = profile_mod.sample_chunk.__wrapped__(
        plan[0], dataset="otel_s3", windows=4, rows_per_window=100
    )
    rendered = normalized(query)

    assert rendered.count("LIMIT 100") == 4
    assert rendered.count("UNION ALL") == 3
    # Two timestamp bounds per window.
    assert len(params) == 8
    assert "stratified sample" in " ".join(stats.notes)


def test_sampling_is_the_first_stage_that_reads_message_text(plan):
    query, _, _ = profile_mod.sample_chunk.__wrapped__(plan[0], dataset="otel_s3")
    assert '"Body"' in normalized(query)


def test_sampling_can_filter_to_problem_severity(plan):
    query, params, _ = profile_mod.sample_chunk.__wrapped__(
        plan[0], dataset="otel_s3", severity="error"
    )
    assert '"SeverityText" = ' in normalized(query)
    assert params.count("error") == 8


def test_sample_stats_report_the_whole_chunk_as_read(plan):
    """Honest cost: a row group is read in full even if few rows are returned."""
    _, _, stats = profile_mod.sample_chunk.__wrapped__(plan[0], dataset="otel_s3")
    assert stats.est_rows_scanned == plan[0].num_rows
    assert stats.est_bytes_read == plan[0].compressed_bytes
    assert stats.approximate is True


# --- Triage scoring ---------------------------------------------------------
def test_question_terms_drop_noise_words():
    terms = triage.question_terms("Why are there payment errors in the service?")
    assert "payment" in terms
    # 'error' and 'service' have their own dedicated signals, so they are stopped.
    assert "errors" not in terms and "error" not in terms
    assert "the" not in terms


def test_profile_only_scoring_can_discard_but_never_promote():
    quiet = ChunkProfile("c1", 614_400, severity_counts={"info": 1000})
    score = triage.score_profile(quiet)
    assert score.verdict == "discard"
    assert score.score < triage.DISCARD_THRESHOLD

    noisy = ChunkProfile("c2", 614_400, severity_counts={"error": 900, "info": 100})
    # Even a fully anomalous chunk cannot reach promotion without term evidence.
    assert triage.score_profile(noisy).verdict == "ambiguous"


def test_sample_scoring_is_deterministic():
    profile = ChunkProfile("c1", 614_400, severity_counts={"error": 50, "info": 50})
    rows = [{"message": "payment gateway timeout"}] * 10
    first = triage.score_sample(profile, rows, "payment timeout")
    second = triage.score_sample(profile, rows, "payment timeout")
    assert first.score == second.score
    assert first.matched_terms == second.matched_terms


def test_a_matching_high_error_chunk_is_promoted():
    profile = ChunkProfile("c1", 614_400, severity_counts={"error": 90, "info": 10})
    rows = [{"message": "payment gateway timeout"}] * 20
    score = triage.score_sample(profile, rows, "payment gateway timeout")
    assert score.verdict == "promote"
    assert score.term_hit_rate == 1.0


def test_an_unrelated_quiet_chunk_is_discarded():
    profile = ChunkProfile("c1", 614_400, severity_counts={"info": 100})
    rows = [{"message": "health check ok"}] * 20
    score = triage.score_sample(profile, rows, "payment gateway timeout")
    assert score.verdict == "discard"


def test_only_the_ambiguous_band_warrants_a_classifier_call():
    profile = ChunkProfile("c1", 614_400, severity_counts={"error": 30, "info": 70})
    rows = [{"message": "payment ok"}] * 5 + [{"message": "health check"}] * 15
    score = triage.score_sample(profile, rows, "payment gateway timeout")
    assert score.verdict == "ambiguous"
    assert triage.needs_classifier(score)

    decisive = triage.score_sample(
        ChunkProfile("c2", 1, severity_counts={"error": 1}),
        [{"message": "payment gateway timeout"}],
        "payment gateway timeout",
    )
    assert not triage.needs_classifier(decisive)


def test_service_overlap_lifts_a_chunk_carrying_the_named_service():
    profile = ChunkProfile(
        "c1",
        614_400,
        severity_counts={"error": 50, "info": 50},
        service_counts={"payment": 90, "frontend": 10},
    )
    rows = [{"message": "upstream call failed"}] * 10
    without = triage.score_sample(profile, rows, "upstream failure")
    with_target = triage.score_sample(
        profile, rows, "upstream failure", target_services=("payment",)
    )
    assert with_target.score > without.score
    assert with_target.service_overlap == pytest.approx(0.9)


def test_ranking_is_stable_for_tied_scores():
    tied = [
        triage.TriageScore("b", 0.5),
        triage.TriageScore("a", 0.5),
        triage.TriageScore("c", 0.9),
    ]
    assert [s.chunk_id for s in triage.rank(tied)] == ["c", "a", "b"]


def test_promotion_selection_is_rank_relative_and_budget_capped():
    """The best chunks with any evidence, not those above a fixed score.

    An absolute threshold was measured to be unreachable: against the real
    corpus a well-matched question scores ~0.25, because the sample spans a
    chunk whose traffic is mostly `info` and whose error rate is a near-uniform
    ~14%. Promotion never fired, so the sweep could rank chunks but never read
    one. `max_promotions` already bounds the spend; this decides which.
    """
    scores = [
        triage.TriageScore("a", 0.30, verdict="ambiguous"),
        triage.TriageScore("b", 0.25, verdict="ambiguous"),
        triage.TriageScore("c", 0.22, verdict="ambiguous"),
    ]
    chosen = triage.select_for_promotion(scores, max_promotions=2)
    assert [s.chunk_id for s in chosen] == ["a", "b"]


def test_a_discarded_chunk_is_never_promoted():
    """The profile pass can rule a chunk out, and that still binds."""
    scores = [
        triage.TriageScore("empty", 0.0, verdict="discard"),
        triage.TriageScore("real", 0.2, verdict="ambiguous"),
    ]
    chosen = triage.select_for_promotion(scores, max_promotions=5)
    assert [s.chunk_id for s in chosen] == ["real"]


def test_a_chunk_with_no_signal_at_all_is_never_promoted():
    """Rank-relative selection still needs a floor, or a sweep over a corpus
    containing nothing relevant would read a chunk anyway."""
    scores = [triage.TriageScore("nothing", 0.0, verdict="ambiguous")]
    assert triage.select_for_promotion(scores, max_promotions=5) == []


# --- Coverage honesty -------------------------------------------------------
def test_coverage_states_the_fractions_actually_examined():
    record = triage.coverage(
        total_chunks=1_624,
        profiled=1_624,
        sampled=40,
        promoted=2,
        rows_in_corpus=1_000_000_000,
        rows_sampled=80_000,
    )
    assert record["fraction_profiled"] == 1.0
    assert record["chunks_promoted"] == 2
    assert record["fraction_rows_sampled"] == pytest.approx(8e-05)
    # The limitation line is mandatory, not optional politeness.
    assert "may exist in chunks that were profiled but not searched" in (
        record["limitation"]
    )


def test_coverage_handles_an_empty_corpus_without_dividing_by_zero():
    record = triage.coverage(0, 0, 0, 0, 0, 0)
    assert record["fraction_profiled"] == 0.0
    assert record["fraction_rows_sampled"] == 0.0


# --- Loop termination -------------------------------------------------------
def test_chunk_lookup_terminates_on_every_moment(plan):
    """``_locate`` is a binary search in a while loop.

    Checked across the boundaries where an off-by-one would stop it narrowing:
    before the first chunk, exactly on each edge, and past the last.
    """
    from datetime import timedelta

    from backend.db.profile import _locate

    chunks = plan
    probes = [chunks[0].ts_lo - timedelta(seconds=5), chunks[-1].ts_hi]
    for chunk in (chunks[0], chunks[1], chunks[len(chunks) // 2], chunks[-1]):
        probes += [chunk.ts_lo, chunk.ts_hi, chunk.ts_lo + timedelta(microseconds=1)]

    for moment in probes:
        found = _locate(chunks, moment)
        if found is not None:
            assert found.ts_lo <= moment < found.ts_hi


def test_chunk_lookup_on_an_empty_map_returns_nothing():
    from datetime import UTC, datetime

    from backend.db.profile import _locate

    assert _locate([], datetime(2025, 9, 23, tzinfo=UTC)) is None
