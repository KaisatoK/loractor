# Grounded answers

Every measured statement is traceable: it carries the result it cites, and every answer ends with an appendix of the executed queries. The rules are the challenge's own — count matching log entries accurately, separate observed facts from possible explanations and recommended checks, and keep the evidence accessible for checking.

## The findings ledger

Below the narrative, code (never the model) appends three labelled groups:

- **Measured** — statements shown by the cited query results. A measured finding that cites no valid result is downgraded to a hypothesis. So is one whose figures the cited results do not show (see the number check below). If a measured finding is backed only by example rows, it is tagged "row-level evidence, not a frequency".
- **Hypotheses** — explanations the logs do not confirm. A measured finding relabelled by the semantic claim check appears here with the reason.
- **Limitations** — what the data does not establish.

Each citation is tagged with its result's kind, so a reader can tell a count from an example at a glance.

## Result kinds

Every tool returns one result envelope. The `kind` says what the rows are, and only some kinds measure:

- `aggregate` and `trend` are **measured frequencies** over the stated scope — database counts and per-bucket counts.
- `derived` rows are **computed in code from measured counts** whose queries are listed (the priority ranking, and the engineer's timeline). A derived result that is not a frequency (the timeline) says so explicitly.
- `search`, `context`, `lookup` and `catalog` rows are **examples or metadata** and never establish how often something happens.

The wording rules follow from that: counts are **log entries**, never "incidents", and a few retrieved examples never establish frequency. Frequency comes only from aggregate, trend or the code-derived ranking.

## Data scope and queries

Every answer ends with a code-built appendix headed **"Data scope and queries"**. It lists the datasets, the time windows and the filters that were applied; which results are measured counts and which are examples; whether each result was truncated; and the executed SQL for every cited result. A derived record instead shows its formula and the result ids it was computed from, so the arithmetic is checkable. The appendix is built from the result envelopes, not from the narrative.

## Inline result tables

Answer tables are written as `[[RESULT:Rn]]` markers, alone on their own line. The UI replaces each marker with the corresponding table from the tool results, so a table shown in an answer is the returned rows themselves, not a re-typed copy. The serializer (`src/backend/pipeline/answers.py`) and the renderer (`src/ui/rendering.py`) are kept in sync.

## Two checks on measured claims

- **The code number check** runs always. Every number in a measured finding must appear in the results it cites — a numeric cell, a result total, or a filter or argument. A sum, share or rounding the model computed itself is not in the results and is not measured, so the finding is downgraded to a hypothesis with the missing figure named. Thousands-grouped totals in the narrative prose are checked the same way. Source: `src/backend/pipeline/claims.py`.
- **The semantic claim check** sends each measured finding that survived the code check, together with the rows it cites, to one classifier request. Each finding gets a verdict of *supports*, *contradicts* or *says_nothing*. A claim whose rows say nothing about it is relabelled as a hypothesis; a contradicted claim is relabelled and the answer is sent back for a rewrite. The gate and the revision policy live in code (`src/backend/pipeline/decision.py`); a classifier failure leaves the findings as the code labelled them.

## The confidence loop

After the answer is written, the confidence stage asks independent questions about whether the evidence is relevant and sufficient, whether an untried tool could close a remaining gap, and whether the answer is grounded, complete and suited to the role. Code composes those answers into a route: revise the evidence, revise the answer, or finish. The loop is bounded by the per-effort revision budget, so a run cannot revise forever.

## See it live

[`docs/worked_investigation.md`](worked_investigation.md) is a full captured run, with the tool calls, executed queries, evidence report, answer and recommended action for each role. Regenerate it with:

```bash
bash ./worked_investigation.sh --topic "<a real issue or recurring pattern>"
```
