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
