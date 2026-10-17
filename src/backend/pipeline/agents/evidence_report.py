"""Evidence report call: ``generate_evidence_report`` maps the agent summary +
captured ``R``-ids onto a validated ``EvidenceReport`` via direct
``deepseek.invoke`` (no ``response_format``; contract/vocab in ``JSON_OUTPUT_SPEC``).
"""

from collections.abc import Sequence

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from backend.pipeline.contracts import EvidenceReport
from backend.pipeline.models import deepseek
from backend.pipeline.prompts import EVIDENCE_REPORT_SYSTEM_PROMPT

# The json output contract, placed in the user message so the system prompt can
# stay ``EVIDENCE_REPORT_SYSTEM_PROMPT``. No schema is transmitted, so it carries
# the word "json", a shape example, and only the rules the example cannot convey.
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
    '      "evidence_ids": ["E1"],\n'
    '      "contradicting_evidence_ids": [],\n'
    '      "next_check": "the one check that would confirm or refute it"\n'
    "    }\n"
    "  ],\n"
    '  "coverage": "what fraction of the data was actually examined, and how"\n'
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
    "evidence item id that exists in the report.\n"
    "- `hypotheses` holds AT MOST 3 items: keep the 3 most relevant and merge or "
    "drop the rest. More than 3 is rejected.\n"
    "- `contradicting_evidence_ids` lists evidence items that speak against the "
    "hypothesis (for example a counterexample query); use [] when none. "
    "`next_check` is one concrete diagnostic step that would confirm or refute "
    "the hypothesis, or null.\n"
    "- Do NOT include a `data_scope` field; it is computed separately.\n"
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
    "<captured results catalog>\n"
    "{catalog}\n"
    "</captured results catalog>\n\n"
    "{format}\n\n"
    "Produce the json evidence report now."
)


def _catalog_lines(results: Sequence[dict]) -> str:
    """Compact ``- R{n}: tool_name(arguments) [kind]`` lines for the captured results.

    Non-dict records are skipped; each record carries its own canonical ``id``.
    The kind tells the formatter whether a result is a measured count or a set
    of examples; ``truncated`` flags results with more matching rows.
    """
    lines = []
    for record in results:
        if not isinstance(record, dict):
            continue
        kind = record.get("kind") or "unknown"
        flag = " truncated" if record.get("truncated") else ""
        lines.append(
            f"- {record['id']}: "
            f"{record.get('tool_name')}({record.get('arguments')}) [{kind}{flag}]"
        )
    return "\n".join(lines)


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
    """Build the validated evidence report from the normalized ``request``, the
    ``role``, the investigator's ``investigation`` summary, and ``results`` (the
    only citable ids). Provider/validation failures propagate uncaught."""
    message = deepseek.invoke(
        _messages(
            request=request,
            role=role,
            investigation=investigation,
            results=results,
        )
    )
    return EvidenceReport.model_validate_json(message.text)


__all__ = [
    "generate_evidence_report",
]
