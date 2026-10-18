"""Divide-and-conquer sweep: cover a billion rows with a laptop's resources.

The problem this solves is a specific one. A user who insists on evidence from
the whole corpus cannot be served by a single query -- 38 GiB of message text is
hours of reading -- but neither are they wrong to ask. What is affordable is a
*graded* pass: look at everything cheaply, look harder at less, read text for
almost nothing.

Four stages, each ~100x narrower than the last:

    1. map        1,624 chunks from Parquet footers.           Metadata only.
    2. profile    one grouped aggregate per window.            No text read.
    3. sample     ~2,000 stratified rows from ranked chunks.   First text read.
    4. promote    index a chunk, digest it, drop it.           ~660 s each.

The economics are the whole design. Stage 2 costs about a second per window and
can see every chunk; stage 4 costs eleven minutes per chunk and can see two or
three. So stages 2 and 3 exist to decide where stage 4 goes, and the sweep's
honesty rests on saying which stage saw what: a chunk that was profiled and
discarded was examined, but not read.

**This loop runs inside one tool call.** Per-effort budgets cap *all* tool calls
at between 6 and 22, to-do writes included, so 1,624 chunks cannot be 1,624
calls. The agent sees two tools -- one that sweeps and one that investigates a
named chunk -- and the iteration happens here, in Python, reporting progress as
it goes.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime

from backend.db import chunks as chunk_mod
from backend.db import profile as profile_mod
from backend.db.budget import PROBLEM_SEVERITIES
from backend.db.datasets import Capability, Dataset, resolve
from backend.db.errors import refuse
from backend.db.promotion import (
    ChunkDigest,
    estimate_promotion_seconds,
    promote_chunk,
)
from backend.db.stats import QueryStats, merge
from backend.pipeline import triage

logger = logging.getLogger(__name__)

# Chunks profiled per aggregate query. A profile covers a time window, so
# batching means one query describes many chunks: at ~1.1 s per chunk this is
# roughly a two-minute window, which stays inside the remote tier's limit.
PROFILE_BATCH_CHUNKS = 120

# Chunks carried from profiling into sampling. Sampling reads message text, so
# this is the first stage with a real per-chunk cost and the first that must be
# rationed.
MAX_SAMPLED_CHUNKS = 12

# Default ceiling on a sweep's wall-clock. Generous enough for profiling to
# cover the corpus, tight enough that an unattended sweep cannot run for hours.
DEFAULT_TIME_BUDGET_SECONDS = 900


@dataclass(frozen=True)
class SweepEvent:
    """One observable step of a sweep.

    The sweep's whole cost lives inside a single tool call, so these events are
    the only window into it. Each carries the stage it came from, a readable
    message, and a ``detail`` payload with the numbers behind it -- which chunk,
    what score, which signal drove the decision.
    """

    stage: str
    """``map``, ``profile``, ``sample``, ``promote``, ``decide`` or ``done``."""

    message: str
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"stage": self.stage, "message": self.message, "detail": self.detail}


@dataclass
class SweepBudget:
    """What a sweep may spend. Every field is a hard stop, not a target."""

    max_profiled_chunks: int = 2_000
    max_sampled_chunks: int = MAX_SAMPLED_CHUNKS
    max_promotions: int = 0
    max_seconds: float = DEFAULT_TIME_BUDGET_SECONDS
    max_rows_scanned: int = 1_000_000_000

    def as_dict(self) -> dict:
        return {
            "max_profiled_chunks": self.max_profiled_chunks,
            "max_sampled_chunks": self.max_sampled_chunks,
            "max_promotions": self.max_promotions,
            "max_seconds": self.max_seconds,
            "max_rows_scanned": self.max_rows_scanned,
        }


@dataclass
class SweepProgress:
    """Counters a caller can watch while the sweep runs."""

    chunks_total: int = 0
    chunks_profiled: int = 0
    chunks_sampled: int = 0
    chunks_promoted: int = 0
    chunks_discarded: int = 0
    rows_scanned: int = 0
    elapsed_seconds: float = 0.0
    stopped_because: str | None = None

    def as_dict(self) -> dict:
        return {
            "chunks_total": self.chunks_total,
            "chunks_profiled": self.chunks_profiled,
            "chunks_sampled": self.chunks_sampled,
            "chunks_promoted": self.chunks_promoted,
            "chunks_discarded": self.chunks_discarded,
            "rows_scanned": self.rows_scanned,
            "elapsed_seconds": round(self.elapsed_seconds, 1),
            "stopped_because": self.stopped_because,
        }


@dataclass
class SweepResult:
    """Everything a sweep produces, including what it did not manage to do."""

    question: str
    dataset: str
    progress: SweepProgress
    budget: SweepBudget
    ranked: tuple[triage.TriageScore, ...] = ()
    digests: tuple[ChunkDigest, ...] = ()
    stats: QueryStats | None = None
    coverage: dict = field(default_factory=dict)
    cursor: int = 0
    notes: tuple[str, ...] = field(default=())
    sampling: tuple[dict, ...] = field(default=())
    """What each sampled chunk's sample supports, with its caveat.

    Separate from ``ranked`` because a score is a decision and this is its
    confidence: the sample is drawn like a quicksort pivot -- cheaply, from a
    chunk nothing is known about -- and an answer leaning on it has to say so.
    """

    events: tuple[SweepEvent, ...] = field(default=())
    """Every step the sweep took, in order.

    Kept on the result as well as streamed, because the stream is gone once the
    run finishes and the decisions are worth auditing afterwards.
    """

    def as_dict(self) -> dict:
        return {
            "question": self.question,
            "dataset": self.dataset,
            "progress": self.progress.as_dict(),
            "budget": self.budget.as_dict(),
            "ranked": [score.as_dict() for score in self.ranked[:20]],
            "digests": [digest.as_dict() for digest in self.digests],
            "coverage": self.coverage,
            "cursor": self.cursor,
            "notes": list(self.notes),
            "query_stats": self.stats.as_dict() if self.stats else None,
            "trace": [event.as_dict() for event in self.events],
            "sampling": list(self.sampling),
        }


# Services the OTel corpus is known to contain, used to spot a question that
# names one. Measured from the corpus rather than invented.
KNOWN_SERVICES = (
    "frontend",
    "payment",
    "email",
    "checkout",
    "currency",
    "cart",
    "shipping",
    "recommendation",
    "product-catalog",
    "ad",
    "quote",
    "accounting",
    "fraud-detection",
    "load-generator",
)


def _mentioned_services(question: str) -> tuple[str, ...]:
    """Known service names appearing in a question.

    Profile scoring can use service overlap but has no text to match against,
    so this is the only question-derived signal available before any message is
    read.
    """
    lowered = question.lower()
    return tuple(name for name in KNOWN_SERVICES if name in lowered)


def _require_sweepable(dataset: Dataset | str) -> Dataset:
    ds = resolve(dataset) if isinstance(dataset, str) else dataset
    if not ds.supports(Capability.CHUNK_SWEEP):
        raise refuse(
            f"Dataset '{ds.id}' cannot be swept.",
            "The sweep addresses a remote corpus by row group; local corpora "
            "are small enough to query directly.",
            code="not_chunkable",
            dataset=ds.id,
        )
    return ds


def estimate_sweep(
    dataset: Dataset | str = "otel_s3", *, budget: SweepBudget | None = None
) -> dict:
    """Price a sweep before running it.

    Task 17 requires a user to confirm this, and the reason is the arithmetic:
    profiling the corpus is minutes, but each promotion is ~11 minutes, so the
    promotion allowance dominates the total by an order of magnitude.
    """
    ds = _require_sweepable(dataset)
    budget = budget or SweepBudget()
    plan = chunk_mod.plan_chunks(ds)

    profiled = min(len(plan), budget.max_profiled_chunks)
    profile_queries = -(-profiled // PROFILE_BATCH_CHUNKS)
    sampled = min(budget.max_sampled_chunks, profiled)

    # Measured: a profile aggregate over a ~2-minute window runs in ~1.5 s, and
    # a stratified sample that reads message text costs a row-group text read.
    profile_seconds = profile_queries * 2.0
    sample_seconds = sampled * 35.0
    promote_seconds = budget.max_promotions * (
        estimate_promotion_seconds(plan[0]) if plan else 660.0
    )

    return {
        "dataset": ds.id,
        "chunks_available": len(plan),
        "chunks_to_profile": profiled,
        "profile_queries": profile_queries,
        "chunks_to_sample": sampled,
        "promotions_allowed": budget.max_promotions,
        "estimated_seconds": round(
            profile_seconds + sample_seconds + promote_seconds, 1
        ),
        "estimated_profile_seconds": round(profile_seconds, 1),
        "estimated_sample_seconds": round(sample_seconds, 1),
        "estimated_promote_seconds": round(promote_seconds, 1),
        "rows_addressable": sum(c.num_rows for c in plan),
        "note": (
            "Profiling reads no message text and scales to the whole corpus. "
            "Promotion reads text and costs about eleven minutes per chunk, so "
            "it dominates any sweep that uses it."
        ),
    }


def sweep(
    question: str,
    *,
    dataset: Dataset | str = "otel_s3",
    budget: SweepBudget | None = None,
    start_at: int = 0,
    severities: tuple[str, ...] = PROBLEM_SEVERITIES,
    progress=None,
) -> SweepResult:
    """Run the graded pass and return digests plus an honest coverage note.

    ``start_at`` resumes from a previous sweep's cursor, so a second invocation
    continues through the corpus instead of re-examining its beginning.
    """
    ds = _require_sweepable(dataset)
    budget = budget or SweepBudget()

    # Service names mentioned in the question, used by profile scoring. Taken
    # from the registry's own vocabulary rather than guessed, so a question
    # naming "payment" biases toward chunks where that service is active.
    target_services = _mentioned_services(question)

    plan = chunk_mod.plan_chunks(ds)
    state = SweepProgress(chunks_total=len(plan))
    started = time.monotonic()
    rows_addressable = sum(c.num_rows for c in plan)
    collected_stats: list[QueryStats] = []
    notes: list[str] = []
    trace: list[SweepEvent] = []
    uncertainties: dict[str, triage.SampleUncertainty] = {}

    def elapsed() -> float:
        return time.monotonic() - started

    def emit(stage: str, message: str, **detail) -> None:
        """Record a step, and report it if anyone is listening.

        Recorded unconditionally: the trace is part of the result, so a sweep
        run without a progress listener is still auditable afterwards.
        """
        event = SweepEvent(stage=stage, message=message, detail=detail)
        trace.append(event)
        if progress is not None:
            progress(event)

    def over_budget() -> str | None:
        if elapsed() > budget.max_seconds:
            return f"time budget of {budget.max_seconds:.0f}s reached"
        if state.rows_scanned > budget.max_rows_scanned:
            return f"scan budget of {budget.max_rows_scanned:,} rows reached"
        return None

    # --- Stage 2: profile, cheaply, as widely as the budget allows ----------
    #
    # Chunks are profiled in time-ordered batches so one aggregate describes
    # many of them, and the cursor advances monotonically. That is what makes
    # resume meaningful: a sweep that stops at chunk 600 has genuinely examined
    # chunks 0-599, and the next can start at 600.
    emit(
        "map",
        f"Mapped {len(plan):,} chunks covering {rows_addressable:,} rows "
        f"from file metadata only.",
        chunks=len(plan),
        rows_addressable=rows_addressable,
        start_at=start_at,
        target_services=list(target_services),
    )

    scored: list[triage.TriageScore] = []
    profiled_by_id: dict[str, profile_mod.ChunkProfile] = {}
    cursor = start_at
    window = plan[start_at : start_at + budget.max_profiled_chunks]

    for offset in range(0, len(window), PROFILE_BATCH_CHUNKS):
        reason = over_budget()
        if reason:
            state.stopped_because = reason
            break

        batch = window[offset : offset + PROFILE_BATCH_CHUNKS]
        if not batch:
            break

        rows = profile_mod.profile_buckets(
            batch[0].ts_lo.isoformat(sep=" "),
            batch[-1].ts_hi.isoformat(sep=" "),
            dataset=ds.id,
            bucket="second",
            by_service=bool(target_services),
        )
        if getattr(rows, "refused", False):
            notes.append(
                f"profiling refused for chunks {batch[0].chunk_id}"
                f"..{batch[-1].chunk_id}: {rows.refusal.reason}"
            )
            cursor += len(batch)
            continue
        if rows.stats:
            collected_stats.append(rows.stats)

        profiles = profile_mod.attach_profiles(tuple(batch), list(rows))
        for chunk, chunk_profile in zip(batch, profiles):
            score = triage.score_profile(
                chunk_profile,
                target_services=target_services,
                problem_severities=severities,
            )
            scored.append(score)
            profiled_by_id[score.chunk_id] = chunk_profile
            state.chunks_profiled += 1
            state.rows_scanned += chunk.num_rows
            if score.verdict == "discard":
                state.chunks_discarded += 1

        cursor += len(batch)
        state.elapsed_seconds = elapsed()
        batch_scores = scored[-len(batch):]
        kept = [x for x in batch_scores if x.verdict != "discard"]
        emit(
            "profile",
            f"Profiled chunks {batch[0].chunk_id}..{batch[-1].chunk_id} "
            f"({state.chunks_profiled:,}/{state.chunks_total:,}); "
            f"kept {len(kept)} of {len(batch)} for sampling.",
            chunks_profiled=state.chunks_profiled,
            chunks_total=state.chunks_total,
            rows_counted=state.rows_scanned,
            kept=len(kept),
            discarded=len(batch) - len(kept),
            best_in_batch=max((x.score for x in batch_scores), default=0.0),
            reads_message_text=False,
        )

    # --- Stage 3: sample the survivors, which is the first text read --------
    survivors = [s for s in triage.rank(scored) if s.verdict != "discard"]
    by_id = {c.chunk_id: c for c in plan}

    sampled_scores: list[triage.TriageScore] = []
    for score in survivors[: budget.max_sampled_chunks]:
        reason = over_budget()
        if reason:
            state.stopped_because = reason
            break

        chunk = by_id.get(score.chunk_id)
        if chunk is None:
            continue

        rows = profile_mod.sample_chunk(
            chunk, dataset=ds.id, windows=profile_mod.SAMPLE_WINDOWS
        )
        if getattr(rows, "refused", False):
            notes.append(f"sampling refused for {chunk.chunk_id}")
            continue
        if rows.stats:
            collected_stats.append(rows.stats)

        chunk_profile = profiled_by_id.get(score.chunk_id)
        if chunk_profile is None:
            continue
        sample_score = triage.score_sample(
            chunk_profile,
            list(rows),
            question,
            target_services=target_services,
            problem_severities=severities,
        )
        sampled_scores.append(sample_score)
        state.chunks_sampled += 1
        state.elapsed_seconds = elapsed()

        # What the sample actually supports. Computed across windows, so a
        # chunk whose parts disagree reports a wide range rather than a
        # confident-looking ratio.
        uncertainty = triage.sample_uncertainty(
            list(rows),
            sample_score.matched_terms or triage.question_terms(question),
            rows_in_chunk=chunk.num_rows,
        )
        uncertainties[chunk.chunk_id] = uncertainty

        # The evaluation itself, not just the outcome: the three weighted
        # signals and the words that matched, so a surprising verdict can be
        # understood rather than guessed at.
        emit(
            "sample",
            f"Sampled {chunk.chunk_id}: {len(rows)} rows read, "
            f"score {sample_score.score:.2f} ({sample_score.verdict}) -- "
            f"{sample_score.reason}",
            **sample_score.as_dict(),
            rows_read=len(rows),
            reads_message_text=True,
            uncertainty=uncertainty.as_dict(),
        )

    ranked = triage.rank(sampled_scores or scored)

    # --- Stage 4: promote the few that earned it ---------------------------
    digests: list[ChunkDigest] = []
    if budget.max_promotions:
        for score in triage.select_for_promotion(ranked, budget.max_promotions):
            reason = over_budget()
            if reason:
                state.stopped_because = reason
                break

            chunk = by_id.get(score.chunk_id)
            if chunk is None:
                continue

            emit(
                "decide",
                f"Promoting {score.chunk_id} (score {score.score:.2f}): "
                f"{score.reason}. Estimated "
                f"{estimate_promotion_seconds(chunk):.0f}s to read its text.",
                **score.as_dict(),
                estimated_seconds=estimate_promotion_seconds(chunk),
            )

            try:
                digest = promote_chunk(
                    chunk,
                    dataset=ds,
                    severities=severities,
                    question=question,
                    triage_score=score.score,
                )
            except Exception as exc:  # noqa: BLE001 - one chunk must not end a sweep
                logger.warning("Promotion of %s failed: %s", score.chunk_id, exc)
                notes.append(f"promotion of {score.chunk_id} failed: {exc}")
                continue

            digests.append(digest)
            state.chunks_promoted += 1
            state.elapsed_seconds = elapsed()
            emit(
                "promote",
                f"Read {digest.chunk_id}: {digest.rows_indexed:,} problem rows "
                f"from {digest.rows_scanned:,} scanned, "
                f"{len(digest.top_templates)} templates, "
                f"{len(digest.exemplars)} exemplar lines kept; "
                f"disk returned after digest.",
                chunk_id=digest.chunk_id,
                rows_indexed=digest.rows_indexed,
                rows_scanned=digest.rows_scanned,
                build_seconds=round(digest.build_seconds, 1),
                bytes_used=digest.bytes_used,
                top_template=(
                    digest.top_templates[0][0] if digest.top_templates else None
                ),
                reads_message_text=True,
            )
    elif ranked:
        notes.append(
            "No promotions were permitted at this effort level, so no message "
            "text was read from the highest-ranked chunks."
        )
        emit(
            "decide",
            f"Ranked {len(ranked)} chunks but read no message text: this "
            "effort level permits no promotions.",
            ranked=len(ranked),
            best_score=ranked[0].score if ranked else 0.0,
            promotions_allowed=0,
        )

    state.elapsed_seconds = elapsed()
    emit(
        "done",
        f"Swept {state.chunks_profiled:,} of {state.chunks_total:,} chunks in "
        f"{state.elapsed_seconds:.0f}s: {state.chunks_sampled} sampled, "
        f"{state.chunks_promoted} read in full."
        + (f" Stopped early: {state.stopped_because}." if state.stopped_because else ""),
        **state.as_dict(),
    )

    for chunk_id, u in uncertainties.items():
        if not u.is_decisive:
            notes.append(f"{chunk_id}: {u.caveat()}")

    coverage = triage.coverage(
        total_chunks=state.chunks_total,
        profiled=state.chunks_profiled,
        sampled=state.chunks_sampled,
        promoted=state.chunks_promoted,
        rows_in_corpus=sum(c.num_rows for c in plan),
        rows_sampled=sum(d.rows_scanned for d in digests)
        + state.chunks_sampled * profile_mod.MAX_SAMPLE_ROWS,
    )

    return SweepResult(
        question=question,
        dataset=ds.id,
        progress=state,
        budget=budget,
        ranked=tuple(ranked),
        digests=tuple(digests),
        stats=merge(collected_stats) if collected_stats else None,
        coverage=coverage,
        cursor=cursor,
        notes=tuple(notes),
        sampling=tuple(
            {"chunk_id": cid, **u.as_dict()} for cid, u in uncertainties.items()
        ),
        events=tuple(trace),
    )


def investigate_chunk(
    chunk_id: str,
    *,
    question: str = "",
    dataset: Dataset | str = "otel_s3",
    severities: tuple[str, ...] = PROBLEM_SEVERITIES,
    progress=None,
) -> dict:
    """Promote and digest one named chunk.

    The companion to :func:`sweep`: the sweep says which chunks look relevant,
    this reads one of them properly. Separate because it is the expensive half,
    and a user should be able to spend that on a chunk they chose.

    ``progress``, when given, is called with a :class:`SweepEvent` before the
    read starts and after the digest, the same events (stage ``promote``) the
    sweep reports for its own promotions.
    """
    ds = _require_sweepable(dataset)
    chunk = chunk_mod.find_chunk(ds, chunk_id)
    if progress is not None:
        progress(
            SweepEvent(
                "promote",
                f"Reading {chunk_id}: indexing its problem rows, then summarising "
                "and dropping them (about eleven minutes).",
                {"chunk_id": chunk_id, "reads_message_text": True},
            )
        )
    digest = promote_chunk(
        chunk, dataset=ds, severities=severities, question=question
    )
    if progress is not None:
        progress(
            SweepEvent(
                "promote",
                f"Read {digest.chunk_id}: {digest.rows_indexed:,} problem rows "
                f"from {digest.rows_scanned:,} scanned; disk returned after digest.",
                {
                    "chunk_id": digest.chunk_id,
                    "rows_indexed": digest.rows_indexed,
                    "rows_scanned": digest.rows_scanned,
                    "build_seconds": round(digest.build_seconds, 1),
                    "reads_message_text": True,
                },
            )
        )
    return {
        "chunk_id": chunk_id,
        "digest": digest.as_dict(),
        "coverage": (
            f"Read message text for chunk {chunk_id} only "
            f"({digest.rows_scanned:,} rows scanned, "
            f"{digest.rows_indexed:,} indexed). Other chunks are unexamined."
        ),
    }
