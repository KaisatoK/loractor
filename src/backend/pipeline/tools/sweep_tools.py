"""The two sweep tools.

Only two, and that is the design. The sweep iterates over up to 1,624 chunks,
but per-effort budgets cap *all* tool calls at between 6 and 22 (to-do writes
included), so the iteration cannot be the agent's. It runs in Python inside one
call; the agent decides whether to sweep and, afterwards, which single chunk is
worth reading properly.

Both are gated, for different reasons. ``sweep_chunks_tool`` returns a cost
estimate unless the caller confirms, because a sweep with promotions is a
multi-minute commitment and the user should agree to it rather than discover it.
``investigate_chunk_tool`` needs no confirmation because its cost is one chunk,
stated in the sweep result that named it.
"""

from __future__ import annotations

from langchain.tools import ToolRuntime, tool

from backend.pipeline.sweep import (
    SweepBudget,
    estimate_sweep,
    investigate_chunk,
    sweep,
)
from backend.pipeline.tools.executor import execute_database_tool
from backend.pipeline.tools.schemas import InvestigateChunk, SweepChunks


@tool(args_schema=SweepChunks)
def sweep_chunks_tool(
    runtime: ToolRuntime,
    question: str,
    dataset: str = "otel_s3",
    max_promotions: int = 0,
    start_at: int = 0,
    confirm_cost: bool = False,
) -> dict:
    """Examine a whole remote corpus in graded passes and rank what looks relevant.

    Use this only when the user insists on evidence from the entire corpus and a
    bounded query will not do. It maps every chunk from Parquet footers, profiles
    them with aggregates that read no message text, samples the most promising,
    and ranks them. With `max_promotions` above zero it then reads message text
    for that many top-ranked chunks and returns verbatim exemplars.

    Call it first with `confirm_cost=false` to get an estimate, show the user
    what it will cost, and only then call again with `confirm_cost=true`.

    The result's `coverage` states what was examined at each depth. Report it:
    a chunk that was profiled was counted, not read, so a sweep can miss
    evidence and the answer must not imply otherwise.

    The result also carries a `trace`: every stage, every chunk's score and the
    three signals behind it, and the reason any chunk was read in full. Use it
    to explain *why* the evidence you have is the evidence you have.

    And it carries `sampling`: for each sampled chunk, the fraction examined and
    a likely range for the match rate. Those samples are cheap guesses at chunks
    nothing was known about -- roughly 0.3% of a chunk, in a handful of windows
    -- so quote the range, not the point estimate, and say when the windows
    disagreed.
    """
    budget = SweepBudget(max_promotions=max_promotions)

    if not confirm_cost:
        return execute_database_tool(
            runtime=runtime,
            operation=f"estimate the cost of sweeping {dataset}",
            function=lambda: _as_estimate(dataset, budget),
            kind="search",
            dataset=dataset,
            complete=True,
            filters={"question": question, "confirm_cost": False},
        )

    return execute_database_tool(
        runtime=runtime,
        operation=(
            f"sweep {dataset} for evidence about {question[:60]!r}"
            + (f" with {max_promotions} promotions" if max_promotions else "")
        ),
        # The sweep is the one tool that reports from inside itself. Its call
        # can run for minutes over 1,624 chunks, and a silent minute is
        # indistinguishable from a hang.
        with_reporter=True,
        function=lambda reporter: _as_rows(
            sweep(
                question,
                dataset=dataset,
                budget=budget,
                start_at=start_at,
                progress=_relay(reporter),
            )
        ),
        kind="search",
        dataset=dataset,
        complete=True,
        filters={
            "question": question,
            "max_promotions": max_promotions,
            "start_at": start_at,
        },
    )


@tool(args_schema=InvestigateChunk)
def investigate_chunk_tool(
    runtime: ToolRuntime,
    chunk_id: str,
    dataset: str = "otel_s3",
    question: str = "",
) -> dict:
    """Read one chunk's message text and return counts, templates, and real lines.

    Use it on a chunk a sweep ranked highly, when the ranking alone is not
    enough and the investigation needs quotable lines. It indexes the chunk,
    summarises it, then drops it, so the cost is paid once and the disk is
    returned.

    Reading one chunk says nothing about the rest of the corpus; the returned
    `coverage` says so explicitly and should be carried into the answer.
    """
    return execute_database_tool(
        runtime=runtime,
        operation=f"read message text for chunk {chunk_id}",
        function=lambda: _single_row(
            investigate_chunk(chunk_id, question=question, dataset=dataset)
        ),
        kind="search",
        dataset=dataset,
        complete=True,
        filters={"chunk_id": chunk_id, "question": question},
    )


def _relay(reporter):
    """Forward sweep events to the tool-progress stream.

    A thin adapter on purpose: the sweep should not know what a tool runtime is,
    and the reporter should not know what a sweep stage is.
    """

    def relay(event):
        reporter(event.message, stage=event.stage, detail=event.detail)

    return relay


def _as_estimate(dataset: str, budget: SweepBudget):
    """Wrap an estimate as a one-row result.

    The executor's contract is row-shaped, and an estimate is genuinely one
    record, so this keeps the display path unchanged rather than adding a
    second result shape for the renderer to learn.
    """
    from backend.db.stats import QueryResult

    estimate = estimate_sweep(dataset, budget=budget)
    estimate["confirm_required"] = True
    estimate["how_to_proceed"] = (
        "Show these figures to the user. If they agree, call again with "
        "confirm_cost=true."
    )
    return QueryResult([estimate], stats=None)


def _as_rows(result):
    """Flatten a sweep result into rows, keeping its stats for cost reporting."""
    from backend.db.stats import QueryResult

    payload = result.as_dict()
    rows = [
        {
            "coverage": payload["coverage"],
            "progress": payload["progress"],
            "cursor": payload["cursor"],
            "notes": payload["notes"],
            # How the sweep reached its conclusions, step by step. The model is
            # told to report coverage; this is what lets a human check it.
            "trace": payload["trace"],
            # What each sample supports. The sample is a cheap guess at a chunk
            # nothing is known about, so its confidence travels with it.
            "sampling": payload["sampling"],
        }
    ]
    rows += [{"ranked_chunk": entry} for entry in payload["ranked"]]
    rows += [{"digest": digest} for digest in payload["digests"]]
    return QueryResult(rows, stats=result.stats)


def _single_row(payload: dict):
    from backend.db.stats import QueryResult

    return QueryResult([payload], stats=None)


__all__ = ["investigate_chunk_tool", "sweep_chunks_tool"]
