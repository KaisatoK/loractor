"""Evidence judgement call: prompt contract and entry point.

The ``find_evidence`` deep agent investigates and finishes with a free-text
summary. The evidence items of the report (tool, parameters, status, errors,
result ids) are built in code from the actual tool calls
(``pipeline/evidence.py``); this module asks the model only for the judgement
on top of them: a ``summary`` and at most three ``hypotheses`` citing the
code-built ``E``-ids. The result is a validated :class:`EvidenceJudgement`,
using a direct ``deepseek.invoke`` call.

It owns the prompt/JSON contract (``JSON_OUTPUT_SPEC``) and the single public
entry point ``generate_evidence_judgement``. Callers (``graph.py``'s
``find_evidence_node``) use that entry point and nothing else. This module does
not catch errors - provider or pydantic validation failures propagate to the
caller - and it performs no citation filtering: the authoritative ``result_id``
validation happens downstream in ``pipeline/answers.py`` and ``ui/rendering.py``.

The call deliberately passes no ``response_format`` — neither
``with_structured_output`` nor a ``response_format`` kwarg. Any structured-output
guarantee collapses the streamed reply to a single delta, so the live progress
panel could not render the model's reasoning token-by-token; without it the
projection streams incrementally. The JSON contract therefore rides in the
prompt text (``JSON_OUTPUT_SPEC``) instead of a transmitted schema — json mode
transmits no schema either, so the contract must enumerate every closed
vocabulary the model cannot otherwise see (for example a hypothesis ``status`` is
exactly ``"supported"``, ``"unsupported"`` or ``"inconclusive"``). The returned message's text is validated
here with ``model_validate_json``; ``message.text`` is the text blocks only, so
the reasoning the model emits alongside the report is excluded from the payload.
"""

import logging
from collections.abc import Sequence

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from backend.pipeline.contracts import Evidence, EvidenceJudgement
from backend.pipeline.evidence import evidence_lines
from backend.pipeline.models import deepseek
from backend.pipeline.prompts import EVIDENCE_REPORT_SYSTEM_PROMPT

# The json output contract. It lives in the user message so the
# ``EVIDENCE_REPORT_SYSTEM_PROMPT`` can stay the system message; no schema is
# transmitted, so the prompt must carry the word "json" and an explicit example
# of the shape. The system prompt already carries the grounding/fidelity rules,
# so this spec keeps the shape example and only the two rules the example
# cannot convey.
logger = logging.getLogger(__name__)

JSON_OUTPUT_SPEC = (
    "Return ONLY one json object and no other text, markdown, or commentary. "
    "The json object MUST match exactly this shape:\n"
    "{\n"
    '  "summary": "concise, evidence-grounded summary of the investigation",\n'
    '  "hypotheses": [\n'
    "    {\n"
    '      "hypothesis": "specific, testable explanation",\n'
    '      "description": "why it is plausible and relevant",\n'
    '      "status": "supported",\n'
    '      "evidence_ids": ["E1"],\n'
    '      "contradicting_evidence_ids": [],\n'
    '      "next_check": "the one check that would confirm or refute it"\n'
    "    }\n"
    "  ],\n"
    '  "coverage": "what fraction of the data was actually examined, and how"\n'
    "}\n\n"
    "Rules for filling the json:\n"
    "- The evidence list below was built from the tool calls that actually ran. "
    "Do not restate it and do not add an `evidence` field; cite items only by "
    "their `E`-id.\n"
    "- Include `hypotheses` ONLY when the request actually required "
    "investigation (a why/what-is-causing question). For a direct data question "
    "(counts, lists, lookups) omit `hypotheses` or make it empty. Each "
    "hypothesis status is \"supported\", \"unsupported\", or \"inconclusive\"; "
    "use \"inconclusive\" whenever the evidence is missing, insufficient, "
    "ambiguous, or conflicting. Cite ONLY the `E`-ids listed in the evidence "
    "list; copy each id exactly and never invent one. An item marked `no rows` "
    "or `failed` collected nothing: do not base a conclusion on it.\n"
    "- `hypotheses` holds AT MOST 3 items: keep the 3 most relevant and merge or "
    "drop the rest. More than 3 is rejected.\n"
    "- `contradicting_evidence_ids` lists evidence items that speak against the "
    "hypothesis (for example a counterexample query); use [] when none. "
    "`next_check` is one concrete diagnostic step that would confirm or refute "
    "the hypothesis, or null.\n"
    "- `coverage` is REQUIRED whenever a tool reported one. Copy the figures "
    "from the tool results rather than describing them loosely: say how many "
    "chunks were profiled, sampled and read, or what time window a query "
    "covered. Omit it only when no tool reported coverage.\n"
    "- Figures taken from a chunk SAMPLE are estimates from a fraction of a "
    "percent of that chunk, drawn from a few windows. Report the range the "
    "tool gives, not the point estimate, and say so when the tool reports the "
    "windows disagreed. A sampled rate stated as a fact is a false precision.\n"
    "- A sweep examines the corpus at three different depths and they are not "
    "interchangeable. A chunk that was profiled was COUNTED, not read; only a "
    "promoted chunk had its message text examined. Never describe a profiled "
    "chunk as searched, and never let a bounded sweep's silence read as "
    "evidence that nothing is there -- say what was not examined."
)

PROMPT_TEMPLATE = (
    "<request>\n"
    "{request}\n"
    "</request>\n"
    "<role>\n"
    "{role}\n"
    "</role>\n"
    "<investigation summary>\n"
    "{investigation}\n"
    "</investigation summary>\n"
    "<evidence list>\n"
    "{evidence}\n"
    "</evidence list>\n\n"
    "{format}\n\n"
    "Produce the json evidence judgement now."
)


def _messages(
    *,
    request: str,
    role: str,
    investigation: str,
    evidence: Sequence[Evidence],
    results: Sequence[dict],
) -> list[BaseMessage]:
    """Build the messages for one evidence-judgement call."""
    lines = evidence_lines(evidence, results).strip()
    prompt = PROMPT_TEMPLATE.format(
        request=request.strip(),
        role=role.strip(),
        investigation=investigation.strip() or "(no summary provided)",
        evidence=lines or "(no database tool was called)",
        format=JSON_OUTPUT_SPEC,
    )
    return [
        SystemMessage(content=EVIDENCE_REPORT_SYSTEM_PROMPT),
        HumanMessage(content=prompt),
    ]


def generate_evidence_judgement(
    *,
    request: str,
    role: str,
    investigation: str,
    evidence: Sequence[Evidence],
    results: Sequence[dict],
) -> EvidenceJudgement:
    """Build and return the validated judgement for one investigation.

    Args:
        request: The normalized user request (the question being answered).
        role: The user's role (name and objective) for tailoring.
        investigation: The investigator's free-text summary of the findings.
        evidence: The code-built evidence items (``evidence.build_evidence``);
            their ids are the only ones the judgement may cite.
        results: The captured tool-result records, used to describe each item.

    Returns the :class:`EvidenceJudgement` parsed from the model's text. A
    provider failure or a pydantic validation error is not caught and
    propagates to the caller.
    """
    message = deepseek.invoke(
        _messages(
            request=request,
            role=role,
            investigation=investigation,
            evidence=evidence,
            results=results,
        )
    )
    usage = getattr(message, "usage_metadata", None) or {}
    logger.info(
        "evidence judgement usage: input_tokens=%s output_tokens=%s",
        usage.get("input_tokens"),
        usage.get("output_tokens"),
    )
    return EvidenceJudgement.model_validate_json(message.text)


__all__ = [
    "generate_evidence_judgement",
]
