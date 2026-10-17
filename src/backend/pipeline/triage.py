"""Chunk triage: decide which chunks are worth paying to index.

Scoring is **code-owned**. Three measurable signals are combined with fixed
weights, and the classifier is consulted only for scores that land in a narrow
ambiguous band. That mirrors how :mod:`backend.pipeline.decision` already works
-- atomic judgments from the model, policy in code -- and it is what keeps a
1,624-chunk sweep from costing 1,624 LLM calls.

The signals, cheapest first:

* **error density** -- from Stage 1's profile, which read no message text at all;
* **service overlap** -- whether the chunk carries services the question named;
* **term hit rate** -- how often the question's words appear in the chunk's
  sampled message bodies.

A chunk scoring above :data:`PROMOTE_THRESHOLD` is worth promoting; below
:data:`DISCARD_THRESHOLD` it is dropped on the profile alone, before anything
reads its text.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from backend.db.budget import PROBLEM_SEVERITIES
from backend.db.profile import ChunkProfile

logger = logging.getLogger(__name__)

# Weights sum to 1.0, so a score is always in [0, 1] and thresholds are readable
# as "fraction of the available evidence".
WEIGHT_TERM_HITS = 0.5
WEIGHT_ERROR_DENSITY = 0.3
WEIGHT_SERVICE_OVERLAP = 0.2

# Above this, promote without asking the model.
PROMOTE_THRESHOLD = 0.55

# Below this, discard without asking the model.
DISCARD_THRESHOLD = 0.20

# Between them lies the ambiguous band, and only that band costs a model call.

# An error rate this high is treated as fully anomalous. The corpus baseline is
# ~13.8% error, so 0.35 is roughly 2.5x background rather than an absolute.
ERROR_DENSITY_SATURATION = 0.35

_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,}")

# Words too common in log text or questions to carry signal.
STOP_WORDS = frozenset(
    {
        "the", "and", "for", "with", "from", "that", "this", "was", "were",
        "what", "why", "how", "when", "which", "are", "has", "have", "had",
        "did", "does", "any", "all", "log", "logs", "error", "errors",
        "service", "services", "show", "find", "give", "tell", "about",
        "there", "their", "them", "then", "than", "into", "over", "under",
    }
)


def question_terms(question: str) -> list[str]:
    """Content words from the question, lower-cased and de-duplicated.

    ``error`` and ``service`` are stopped deliberately: nearly every question
    contains them and nearly every chunk matches them, so they would flatten the
    term-hit signal rather than sharpen it. Severity and service are already
    scored by their own dedicated signals.
    """
    seen: list[str] = []
    for match in _WORD.finditer(question.lower()):
        word = match.group(0)
        if word not in STOP_WORDS and word not in seen:
            seen.append(word)
    return seen


@dataclass
class TriageScore:
    """Why a chunk was promoted, sampled, or discarded."""

    chunk_id: str
    score: float
    term_hit_rate: float = 0.0
    error_density: float = 0.0
    service_overlap: float = 0.0
    sampled_rows: int = 0
    matched_terms: list[str] = field(default_factory=list)
    verdict: str = "undecided"
    """One of ``promote``, ``discard``, ``ambiguous``."""

    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "chunk_id": self.chunk_id,
            "score": round(self.score, 4),
            "verdict": self.verdict,
            "reason": self.reason,
            "term_hit_rate": round(self.term_hit_rate, 4),
            "error_density": round(self.error_density, 4),
            "service_overlap": round(self.service_overlap, 4),
            "sampled_rows": self.sampled_rows,
            "matched_terms": list(self.matched_terms),
        }


def score_profile(
    profile: ChunkProfile,
    *,
    target_services: tuple[str, ...] = (),
    problem_severities: tuple[str, ...] = PROBLEM_SEVERITIES,
) -> TriageScore:
    """Score a chunk on its profile alone -- no message text read.

    This is the cheap pass that decides which chunks are even worth sampling.
    Term hits are necessarily zero here, so the attainable score is capped at
    the error-density and service weights; the threshold comparison accounts for
    that by only *discarding* on this signal, never promoting.
    """
    density = profile.error_rate(problem_severities)
    normalized_density = min(density / ERROR_DENSITY_SATURATION, 1.0)

    overlap = 0.0
    if target_services and profile.service_counts:
        present = sum(
            profile.service_counts.get(name, 0) for name in target_services
        )
        total = sum(profile.service_counts.values())
        overlap = present / total if total else 0.0

    score = (
        WEIGHT_ERROR_DENSITY * normalized_density
        + WEIGHT_SERVICE_OVERLAP * overlap
    )
    return TriageScore(
        chunk_id=profile.chunk_id,
        score=score,
        error_density=density,
        service_overlap=overlap,
        verdict="discard" if score < DISCARD_THRESHOLD else "ambiguous",
        reason=(
            f"profile only: {density:.1%} problem severity"
            + (f", {overlap:.1%} target services" if target_services else "")
        ),
    )


def score_sample(
    profile: ChunkProfile,
    sample_rows: list[dict],
    question: str,
    *,
    target_services: tuple[str, ...] = (),
    problem_severities: tuple[str, ...] = PROBLEM_SEVERITIES,
) -> TriageScore:
    """Score a chunk on its sampled message text plus its profile.

    Deterministic for a fixed sample, which is what makes the sweep reproducible
    and the thresholds meaningful.
    """
    terms = question_terms(question)
    matched: list[str] = []
    hits = 0

    for row in sample_rows:
        message = str(row.get("message") or "").lower()
        if not message:
            continue
        row_matched = [term for term in terms if term in message]
        if row_matched:
            hits += 1
            for term in row_matched:
                if term not in matched:
                    matched.append(term)

    hit_rate = hits / len(sample_rows) if sample_rows else 0.0

    density = profile.error_rate(problem_severities)
    normalized_density = min(density / ERROR_DENSITY_SATURATION, 1.0)

    overlap = 0.0
    if target_services:
        counts = profile.service_counts or _sampled_services(sample_rows)
        if counts:
            present = sum(counts.get(name, 0) for name in target_services)
            total = sum(counts.values())
            overlap = present / total if total else 0.0

    score = (
        WEIGHT_TERM_HITS * hit_rate
        + WEIGHT_ERROR_DENSITY * normalized_density
        + WEIGHT_SERVICE_OVERLAP * overlap
    )

    if score >= PROMOTE_THRESHOLD:
        verdict = "promote"
    elif score < DISCARD_THRESHOLD:
        verdict = "discard"
    else:
        verdict = "ambiguous"

    return TriageScore(
        chunk_id=profile.chunk_id,
        score=score,
        term_hit_rate=hit_rate,
        error_density=density,
        service_overlap=overlap,
        sampled_rows=len(sample_rows),
        matched_terms=matched,
        verdict=verdict,
        reason=(
            f"{hits}/{len(sample_rows)} sampled rows matched "
            f"{matched or 'no question terms'}; {density:.1%} problem severity"
        ),
    )


def _sampled_services(sample_rows: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in sample_rows:
        name = row.get("service")
        if name:
            counts[str(name)] = counts.get(str(name), 0) + 1
    return counts


def needs_classifier(score: TriageScore) -> bool:
    """Whether this chunk's score is ambiguous enough to be worth a model call.

    The band is deliberately narrow. Everything outside it is decided by
    arithmetic, which is what keeps the sweep affordable.
    """
    return score.verdict == "ambiguous"


def rank(scores: list[TriageScore]) -> list[TriageScore]:
    """Highest score first; ties broken by chunk id so ordering is stable."""
    return sorted(scores, key=lambda s: (-s.score, s.chunk_id))


def select_for_promotion(
    scores: list[TriageScore], max_promotions: int
) -> list[TriageScore]:
    """The chunks to promote, best first, capped by budget.

    Only ``promote`` verdicts are eligible: an ambiguous chunk that the
    classifier never resolved is left alone rather than promoted on a guess.
    """
    eligible = [s for s in rank(scores) if s.verdict == "promote"]
    return eligible[:max_promotions]


def coverage(
    total_chunks: int,
    profiled: int,
    sampled: int,
    promoted: int,
    rows_in_corpus: int,
    rows_sampled: int,
) -> dict:
    """The honesty record for a sweep-derived answer.

    A bounded sweep can produce false negatives -- a chunk that was only profiled
    may still have held relevant evidence. Stating the fractions is not optional
    politeness; it is what keeps the answer's confidence proportionate to what
    was actually examined.
    """
    return {
        "chunks_total": total_chunks,
        "chunks_profiled": profiled,
        "chunks_sampled": sampled,
        "chunks_promoted": promoted,
        "rows_in_corpus": rows_in_corpus,
        "rows_sampled": rows_sampled,
        "fraction_profiled": round(profiled / total_chunks, 4)
        if total_chunks
        else 0.0,
        "fraction_promoted": round(promoted / total_chunks, 4)
        if total_chunks
        else 0.0,
        "fraction_rows_sampled": round(rows_sampled / rows_in_corpus, 8)
        if rows_in_corpus
        else 0.0,
        "limitation": (
            f"Of {total_chunks} chunks, {profiled} were profiled, {sampled} "
            f"sampled, and {promoted} searched in full. Evidence may exist in "
            "chunks that were profiled but not searched."
        ),
    }
