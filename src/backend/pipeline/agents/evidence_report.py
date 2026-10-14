"""JSON-mode evidence report: runnable, prompt contract, and entry point.

The ``find_evidence`` deep agent investigates and finishes with a free-text
summary; it does not emit structured data itself. This module maps that summary
plus the compact catalog of captured ``R``-ids onto a validated
:class:`EvidenceReport`, using DeepSeek JSON mode.

It owns exactly three things: the module-level json-mode structured-output
runnable (``evidence_report_formatter``), the prompt/JSON contract that shapes the
model's output, and the single public entry point ``generate_evidence_report``.
Callers (``graph.py``'s ``find_evidence_node``) use that entry point and nothing
else. This module does not catch errors — provider or pydantic validation
failures propagate to the caller — and it performs no citation filtering: the
authoritative ``result_id`` validation happens downstream in
``pipeline/answers.py`` and ``ui/rendering.py``.

DeepSeek's JSON mode sets ``response_format={"type": "json_object"}`` and cannot
be steered with a forced ``tool_choice`` (``json_schema``/``ToolStrategy`` are
rejected by the provider), so ``method="json_mode"`` is used and the prompt
carries both the word "json" and an explicit JSON example of the expected shape
— the provider's documented requirements for this mode.
"""

from collections.abc import Sequence

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from backend.pipeline.contracts import EvidenceReport
from backend.pipeline.models import deepseek
from backend.pipeline.prompts import EVIDENCE_REPORT_SYSTEM_PROMPT

# Bound once at import: the JSON-mode structured-output runnable that
# ``generate_evidence_report`` invokes. DeepSeek JSON mode is selected by
# ``response_format`` (no tools).
evidence_report_formatter = deepseek.with_structured_output(
    EvidenceReport, method="json_mode"
)

# The json output contract. It lives in the user message so the
# ``EVIDENCE_REPORT_SYSTEM_PROMPT`` can stay the system message; DeepSeek's json
# mode only requires the word "json" and an explicit example in the messages.
# The system prompt already carries the grounding/fidelity rules, so this spec
# keeps the shape example and only the two rules the example cannot convey.
JSON_OUTPUT_SPEC = (
    "Return ONLY one json object and no other text, markdown, or commentary. "
    "The json object MUST match exactly this shape:\n"
    "{\n"
    '  "summary": "concise, evidence-grounded summary of the investigation",\n'
    '  "evidence": [\n'
    "    {\n"
    '      "id": "E1",\n'
    '      "name": "short evidence name",\n'
    '      "description": "why this evidence is needed and what it evaluates",\n'
    '      "tool_used": [\n'
    "        {\n"
    '          "name": "exact tool name",\n'
    '          "parameters": {"param": "value"},\n'
    '          "status": "success",\n'
    '          "error_message": null\n'
    "        }\n"
    "      ],\n"
    '      "availability": "available",\n'
    '      "result_ids": ["R1"]\n'
    "    }\n"
    "  ],\n"
    '  "hypotheses": [\n'
    "    {\n"
    '      "hypothesis": "specific, testable explanation",\n'
    '      "description": "why it is plausible and relevant",\n'
    '      "status": "supported",\n'
    '      "evidence_ids": ["E1"]\n'
    "    }\n"
    "  ]\n"
    "}\n\n"
    "Rules for filling the json:\n"
    "- `result_ids` must cite ONLY the `R`-ids that appear in the captured "
    "results catalog supplied below. Copy the id exactly. Never invent an id. "
    "Use an empty list when nothing was captured.\n"
    "- `tool_used[].status` is exactly \"success\" or \"failure\"; never "
    "\"failed\" or any other synonym. Set `error_message` to null whenever the "
    "status is \"success\".\n"
    "- Include `hypotheses` ONLY when the request actually required "
    "investigation (a why/what-is-causing question). For a direct data question "
    "(counts, lists, lookups) omit `hypotheses` or make it empty. Each "
    "hypothesis status is \"supported\", \"unsupported\", or \"inconclusive\"; "
    "use \"inconclusive\" whenever the evidence is missing, insufficient, "
    "ambiguous, or conflicting. Every `evidence_ids` entry must refer to an "
    "evidence item id that exists in the report."
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
    "<captured results catalog>\n"
    "{catalog}\n"
    "</captured results catalog>\n\n"
    "{format}\n\n"
    "Produce the json evidence report now."
)


def _catalog_lines(results: Sequence[dict]) -> str:
    """Compact ``- R{n}: tool_name(arguments)`` lines for the captured results.

    Non-dict records are skipped; each record carries its own canonical ``id``.
    """
    return "\n".join(
        f"- {record['id']}: {record.get('tool_name')}({record.get('arguments')})"
        for record in results
        if isinstance(record, dict)
    )


def _messages(
    *, request: str, role: str, investigation: str, results: Sequence[dict]
) -> list[BaseMessage]:
    """Build the JSON-mode messages for one evidence-report call."""
    catalog = _catalog_lines(results)
    catalog = catalog.strip() or "(no results were captured)"
    prompt = PROMPT_TEMPLATE.format(
        request=request.strip(),
        role=role.strip(),
        investigation=investigation.strip() or "(no summary provided)",
        catalog=catalog,
        format=JSON_OUTPUT_SPEC,
    )
    return [
        SystemMessage(content=EVIDENCE_REPORT_SYSTEM_PROMPT),
        HumanMessage(content=prompt),
    ]


def generate_evidence_report(
    *, request: str, role: str, investigation: str, results: Sequence[dict]
) -> EvidenceReport:
    """Build and return the validated evidence report for one investigation.

    Args:
        request: The normalized user request (the question being answered).
        role: The user's role (name and objective) for tailoring.
        investigation: The investigator's free-text summary of the findings and
            the tools it used.
        results: The captured tool-result records, each carrying its canonical
            ``id``; those ids are the only ones the report may cite.

    Returns the :class:`EvidenceReport` unchanged. A provider failure or a
    pydantic validation error is not caught and propagates to the caller.
    """
    return evidence_report_formatter.invoke(
        _messages(
            request=request,
            role=role,
            investigation=investigation,
            results=results,
        )
    )


__all__ = [
    "evidence_report_formatter",
    "generate_evidence_report",
]
