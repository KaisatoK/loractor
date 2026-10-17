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
    # Discard on *absence of signal*, not on an absolute threshold.
    #
    # Measured against the real corpus, comparing this score to
    # DISCARD_THRESHOLD discarded every chunk and left the sweep inert. The two
    # numbers are not commensurable: a profile-only score can reach at most
    # WEIGHT_ERROR_DENSITY + WEIGHT_SERVICE_OVERLAP (0.5), and typically far
    # less, while DISCARD_THRESHOLD is calibrated for the full score including
    # the 0.5 that term hits contribute. The OTel corpus carries a near-uniform
    # ~14% error rate, so every chunk scored 0.12 and every chunk was dropped
    # before a single line was read.
    #
    # What this pass can honestly conclude is narrower: it has read no message
    # text, so it cannot tell whether a chunk is *relevant*, only whether it
    # could possibly be. A chunk with no problem-severity rows at all cannot
    # explain a failure; a chunk with none of the named services cannot either.
    # Everything else is undecided, and ranking chooses which to sample.
    if density <= 0.0:
        verdict, reason = "discard", "profile only: no problem-severity rows"
    elif target_services and overlap <= 0.0:
        verdict, reason = (
            "discard",
            f"profile only: none of {', '.join(target_services)} present",
        )
    else:
        verdict, reason = (
            "ambiguous",
            f"profile only: {density:.1%} problem severity"
            + (f", {overlap:.1%} target services" if target_services else ""),
        )

    return TriageScore(
        chunk_id=profile.chunk_id,
        score=score,
        error_density=density,
        service_overlap=overlap,
        verdict=verdict,
        reason=reason,
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


# Weakest signal a chunk may have and still be worth reading in full.
#
# A floor, not a threshold: it rules out chunks with no evidence at all rather
# than trying to express "relevant enough" as a number.
MIN_PROMOTION_SIGNAL = 0.05


def select_for_promotion(
    scores: list[TriageScore], max_promotions: int
) -> list[TriageScore]:
    """The best chunks to read in full, capped by budget.

    Selection is **rank-relative**, which is a correction of something measured.
    The first version promoted only ``promote`` verdicts, i.e. scores above
    PROMOTE_THRESHOLD (0.55). Against the real corpus nothing ever reached it:
    a well-matched question on part_000 scores ~0.25, because the sample is
    spread across a chunk whose traffic is mostly ``info`` and whose error rate
    is a near-uniform ~14%. Term hits are the only discriminating signal, and
    they are diluted by design -- a specific failure *is* a minority of traffic.

    So an absolute threshold is the wrong instrument. ``max_promotions`` already
    bounds the spend; what this has to decide is *which* chunks, and "the
    highest-ranked ones with any evidence at all" is both answerable and honest.
    A chunk the profile pass discarded, or one with no signal whatsoever, is
    still never promoted.

    The verdict vocabulary is unchanged, because it still describes confidence
    usefully in the digest and in the trace -- it is just no longer the gate.
    """
    eligible = [
        score
        for score in rank(scores)
        if score.verdict != "discard" and score.score >= MIN_PROMOTION_SIGNAL
    ]
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


# --- How much to trust a sample ---------------------------------------------
#
# The sample is drawn like a quicksort pivot: cheaply, from a chunk nothing is
# yet known about, and with no claim to be representative. That is a reasonable
# way to spend a fixed budget, but it has to be *said* rather than papered over
# by quoting "467 of 2000 rows matched" as though it were a measurement of the
# chunk.
#
# Two things make the raw ratio misleading:
#
# * it is a **cluster** sample -- 250 consecutive rows from each of 8 points --
#   and consecutive log lines are strongly correlated, since one request or one
#   error cascade emits many. So the independent observations number about 8,
#   not 2,000, and the spread *between* windows is the only honest basis for a
#   confidence interval.
# * it covers ~0.33% of a chunk, so a pattern confined to the unsampled 99.67%
#   is invisible to it.


@dataclass(frozen=True)
class SampleUncertainty:
    """What a chunk sample does and does not support concluding."""

    rows_sampled: int
    rows_in_chunk: int
    windows: int
    hit_rate: float
    """Fraction of sampled rows matching the question, pooled."""

    window_rates: tuple[float, ...] = ()
    """Per-window hit rates. The spread between these is the real uncertainty."""

    standard_error: float = 0.0
    """Standard error of the mean *across windows*, not across rows."""

    @property
    def sampled_fraction(self) -> float:
        return self.rows_sampled / self.rows_in_chunk if self.rows_in_chunk else 0.0

    @property
    def interval(self) -> tuple[float, float]:
        """Approximate 95% interval for the hit rate, clamped to [0, 1].

        Two standard errors, computed between windows. Wide on purpose: with
        eight clusters it genuinely is.
        """
        margin = 2.0 * self.standard_error
        return (
            max(0.0, self.hit_rate - margin),
            min(1.0, self.hit_rate + margin),
        )

    @property
    def is_decisive(self) -> bool:
        """Whether the windows agree enough to lean on the estimate.

        False when the interval spans more than half its own width either side
        -- i.e. the windows disagree about what the chunk contains, which is
        exactly when a single ratio would mislead.
        """
        low, high = self.interval
        return self.hit_rate > 0 and (high - low) < self.hit_rate

    def as_dict(self) -> dict:
        low, high = self.interval
        return {
            "rows_sampled": self.rows_sampled,
            "rows_in_chunk": self.rows_in_chunk,
            "sampled_fraction": round(self.sampled_fraction, 6),
            "windows": self.windows,
            "hit_rate": round(self.hit_rate, 4),
            "hit_rate_interval": [round(low, 4), round(high, 4)],
            "window_rates": [round(r, 4) for r in self.window_rates],
            "standard_error": round(self.standard_error, 4),
            "decisive": self.is_decisive,
            "caveat": self.caveat(),
        }

    def caveat(self) -> str:
        """The sentence an answer must carry when it leans on this sample."""
        low, high = self.interval
        spread = (
            f"{low:.0%}-{high:.0%}" if self.standard_error else f"{self.hit_rate:.0%}"
        )
        return (
            f"Sampled {self.rows_sampled:,} of {self.rows_in_chunk:,} rows "
            f"({self.sampled_fraction:.2%}) as {self.windows} spread windows. "
            f"Match rate {self.hit_rate:.0%}, likely range {spread}. "
            "Consecutive log lines are correlated, so this rests on about "
            f"{self.windows} independent observations rather than "
            f"{self.rows_sampled:,}"
            + (
                "; the windows disagree, so treat the rate as indicative only."
                if not self.is_decisive
                else "."
            )
        )


def sample_uncertainty(
    sample_rows: list[dict],
    terms: list[str] | tuple[str, ...],
    *,
    rows_in_chunk: int,
) -> SampleUncertainty:
    """Measure how much the sample actually supports.

    Hit rates are computed per window and then averaged, so the reported
    uncertainty reflects disagreement between parts of the chunk. Pooling all
    rows together would produce a tight interval around a number that one
    unusual window could have dominated.
    """
    by_window: dict[object, list[dict]] = {}
    for row in sample_rows:
        by_window.setdefault(row.get("sample_window", 0), []).append(row)

    lowered = [t.lower() for t in terms]

    def hits(rows: list[dict]) -> float:
        if not rows:
            return 0.0
        matched = sum(
            1
            for row in rows
            if any(term in str(row.get("message", "")).lower() for term in lowered)
        )
        return matched / len(rows)

    rates = tuple(hits(rows) for rows in by_window.values())
    pooled = hits(sample_rows)

    error = 0.0
    if len(rates) > 1:
        mean = sum(rates) / len(rates)
        variance = sum((r - mean) ** 2 for r in rates) / (len(rates) - 1)
        error = (variance / len(rates)) ** 0.5

    return SampleUncertainty(
        rows_sampled=len(sample_rows),
        rows_in_chunk=rows_in_chunk,
        windows=len(rates),
        hit_rate=pooled,
        window_rates=rates,
        standard_error=error,
    )
