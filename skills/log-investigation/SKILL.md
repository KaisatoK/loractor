---
name: log-investigation
description: Investigate a reported failure or recurring problem in the logs using testable hypotheses and evidence (root-cause analysis). Use when the question asks WHY or WHAT is causing an issue, not for direct counts, lists, or lookups.
---

# Log Investigation

## Overview
Turn a vague "why is this happening?" into specific, evidence-backed explanations, while leaving direct data questions as plain lookups.

## Instructions
- Attach hypotheses only when explaining a problem: at most the hypothesis budget stated in the request's effort guidance, non-duplicative, plausible, testable hypotheses, each with the evidence it needs.
- Call the database tools yourself for each evidence item (one focused query per item). For a simple lookup, just request the data.
- Judge each hypothesis from the summaries returned: supported / unsupported / inconclusive (missing, insufficient, or conflicting evidence => inconclusive).
- Ground everything in observed results; never fabricate; a failed tool call proves nothing.
- Finish with a thorough summary of the findings and the tools you used (with their parameters and success/failure): always cover the `summary` and the `evidence` items, and include `hypotheses` only when you followed this workflow.

## Working at scale

Datasets range from a few thousand rows to tens of billions held as Parquet in
object storage. The same tools serve both, but on a large corpus the order you
use them decides whether an investigation costs a second or a minute.

**Always narrow before you read text.** A severity count reads two or three
columns; a message search reads log bodies. Find the interesting window with a
count, then search inside it.

1. **Start with a count, not a search.** `count_by_category_tool` is the
   cheapest aggregate there is — it never touches message text. Use it to find
   which minute, service, or severity actually matters.
2. **Then narrow the window.** Follow up with a trend or a tighter count to
   locate the spike inside the window you started with.
3. **Then search text, on an indexed dataset.** `search_logs_tool` reads an
   inverted index. It is refused on the raw remote corpus, which has none — so
   aggregate on the remote corpus to find the window, and search the indexed
   slice to read lines.
4. **Discover filter values before filtering.** `get_data_tool` returns a
   field's most common values with counts, ranked by volume. Do not guess a
   service name.

### Refusals are directions, not dead ends

A tool may answer `status: refused` with a `reason` and a `suggestion`. That
means the query was never run — usually because the time window was too wide,
missing, or the dataset cannot do what was asked. It costs almost nothing and it
tells you what would work. **Follow the suggestion and retry.** Do not abandon
the line of enquiry, and do not report a refusal as evidence of absence.

Common ones:
- *window too wide* — narrow it to the stated limit and retry.
- *severity case* — the corpus uses its own casing; use the value named.
- *unsupported capability* — that corpus has no index or no primary key; use a
  dataset that does, as named in the suggestion.
- *high cardinality* — the field has too many distinct values to list (trace
  ids); filter by service or search the text instead.

### Approximate results must be labelled

Large corpora do not promise exact counts. A result whose `query_stats` has
`approximate: true` is an estimate: say so in the answer ("approximately
870,000 error rows"). Never present an approximate aggregate as an exact
figure, and never add approximate numbers together and present the total as
exact.

### Report what you actually covered

Every tool result carries `query_stats` — rows and bytes read, row groups
pruned, elapsed time. Use those figures when describing the investigation's
scope, and state the limits of what you examined. On a billion-row corpus you
will have read a small fraction of it; a finding drawn from one minute of a
24-hour day is evidence about that minute, and the answer should say so rather
than generalising silently.

## Sweeping a whole corpus

Available only at Extreme and Brutal effort, and only worth reaching for when
the user insists on evidence from the entire corpus and a bounded query will not
satisfy them. It is not a better search — it is a way to spend a lot of time
deliberately.

The reason it is structured in stages is that the stages differ in cost by
orders of magnitude:

| Stage | What it reads | Cost |
| --- | --- | --- |
| map | Parquet footers | negligible |
| profile | timestamp and severity columns | ~1s per window, covers everything |
| sample | ~2,000 rows of message text per chunk | ~35s per chunk |
| promote | all message text in one chunk | **~11 minutes per chunk** |

So profiling can look at all 1,624 chunks, and promotion can look at two or
three. Stages 2 and 3 exist to decide where stage 4 goes.

**Always price it first.** Call `sweep_chunks_tool` with `confirm_cost=false`,
show the user the estimate, and only proceed with `confirm_cost=true` once they
agree. A sweep with four promotions is three quarters of an hour.

**Report coverage, and respect the difference between the depths.** A chunk that
was profiled was counted, not read. If a sweep profiled 1,624 chunks, sampled 12
and promoted 2, say exactly that. Do not write "I searched the corpus" — a
bounded sweep produces false negatives by construction, and an answer that hides
its own limits invites the reader to trust it further than the evidence allows.

**Prefer a narrowed query when one exists.** If the question can be answered by
counting over a five-minute window, do that instead: it costs a second and the
result is exact.

### Explaining a sweep

A sweep result carries a `trace` alongside its `coverage`: every stage it ran,
each sampled chunk's score with the three signals behind it (term hits, error
density, service overlap), and the reason any chunk was read in full.

Use it. "Chunk 412 was read because 6 of 8 sampled rows matched 'charge card'
and its error density was 31%" is an explanation; "I searched the corpus" is
not, and is also false. When a sweep found little, the trace is what
distinguishes *nothing was there* from *nothing was looked at closely*.
