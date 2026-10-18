"""Serialize structured answers into the marker text the UI renders.

The answer agent returns an :class:`~backend.pipeline.contracts.AnswerOutput`:
role-narrative blocks (prose and tables), labelled ``findings`` and
``recommendations``. The UI's rendering protocol consumes a plain string in
which a table is a ``[[RESULT:Rx]]`` marker alone on its own line. This module
is the single place that turns the one into the other, so the marker text is
always machine-generated and well formed.

Everything below the narrative is built in code, never by a model:

* **Findings**, grouped as Measured / Hypotheses / Limitations. A ``measured``
  finding that cites no valid result id is downgraded to a hypothesis, and each
  citation is tagged with its result kind (counts vs examples).
* **Recommended next steps**, with their reason, supporting findings and owner.
* **Data scope and queries** (``APPENDIX_HEADING``): datasets, windows,
  filters, which results are measured vs examples, truncation, and the executed
  SQL per result. ``strip_appendix`` removes it before an answer is fed back to
  the classifiers as conversation context.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from typing import Any

from backend.pipeline.claims import unsupported_numbers
from backend.pipeline.contracts import AnswerTable, AnswerText
from backend.pipeline.data_scope import build_data_scope
from backend.pipeline.tools.envelope import MEASURED_KINDS

logger = logging.getLogger(__name__)

# Canonical, well-formed marker written for each table block.
MARKER_TEMPLATE = "[[RESULT:{result_id}]]"

# Tolerant matcher for marker-like text the model may still echo inside a prose
# block (mirrors the UI's MARKER_RE). Such text is stripped so a stray marker can
# never reach the renderer from a prose block.
_MARKER_RE = re.compile(
    r"[\[【［]{1,2}\s*(?:RESULT\s*:\s*)?(R\d+)\s*[\]】］]{1,2}"
)

APPENDIX_HEADING = "#### Data scope and queries"

# The headings of the code-built sections. Read by the renderer below and by the
# MCP server's role prompts (``answer_layout``), so an external agent is asked
# for exactly the layout the chatbot produces.
FINDING_TITLES = {
    "measured": "**Measured** (shown by the cited query results)",
    "hypothesis": "**Hypotheses** (not confirmed by the logs)",
    "limitation": "**Limitations** (what the data does not establish)",
}
RECOMMENDATIONS_TITLE = "**Recommended next steps**"
MAX_QUERY_CHARS = 1500

_KIND_LABEL = {
    "aggregate": "count",
    "trend": "trend",
    "derived": "computed",
    "search": "examples",
    "context": "nearby events",
    "lookup": "record",
    "catalog": "value list",
    "test": "test",
}


def serialize_answer(
    answer: Any,
    valid_ids: set[str],
    records: Sequence[dict] | None = None,
    notes: Sequence[str] = (),
    cost_note: str = "",
    claim_verdicts: Mapping[int, dict] | None = None,
) -> str:
    """Render the narrative, the labelled ledger and the scope appendix.

    Text blocks become prose; table blocks become a ``[[RESULT:Rx]]`` marker on
    its own line. Table blocks whose id is absent from ``valid_ids`` are dropped
    and repeated ids are collapsed. Findings, recommendations and (when
    ``records`` are given) the data-scope appendix are appended by code.
    ``notes`` are limits of what the answer model saw; they go in the appendix.

    ``cost_note`` is appended after the recommendations, before the appendix. It
    is computed off-model from the tool results' own ``query_stats`` (see
    :mod:`backend.pipeline.cost`), so what the answer claims about data volume is
    measured rather than asserted, and it persists with the message instead of
    living only in the UI.

    ``claim_verdicts`` maps a finding's position in ``findings`` to its claim
    check (``decision.claim_verdicts``): a measured finding whose cited results
    say nothing about it, or contradict it, is shown as a hypothesis with that
    reason instead of as a measurement.
    """
    pieces = _narrative(answer, valid_ids)
    by_id = {r.get("id"): r for r in records or [] if isinstance(r, dict)}
    findings = _findings_section(
        _read(answer, "findings"), valid_ids, by_id, claim_verdicts or {}
    )
    if findings:
        pieces.append(findings)
    steps = _recommendations_section(_read(answer, "recommendations"))
    if steps:
        pieces.append(steps)
    if cost_note:
        pieces.append(cost_note)
    if records:
        pieces.append(render_appendix(records, notes))
    return "\n\n".join(pieces)


def strip_appendix(text: str) -> str:
    """The answer without its code-built data-scope appendix."""
    head, _, _ = (text or "").partition(APPENDIX_HEADING)
    return head.rstrip()


# --- narrative ------------------------------------------------------------


def _narrative(answer: Any, valid_ids: set[str]) -> list[str]:
    pieces: list[str] = []
    seen_tables: set[str] = set()
    for block in _read(answer, "blocks"):
        kind, text, result_id = _read_block(block)
        if kind == "table":
            result_id = (result_id or "").strip()
            if result_id in valid_ids and result_id not in seen_tables:
                seen_tables.add(result_id)
                pieces.append(MARKER_TEMPLATE.format(result_id=result_id))
        else:
            prose = _clean(text)
            if prose:
                pieces.append(prose)
    return pieces


def _clean(text: str | None) -> str:
    return re.sub(r"[ \t]{2,}", " ", _MARKER_RE.sub("", text or "")).strip()


# --- findings & recommendations -------------------------------------------


def _citation(result_id: str, by_id: dict[str, dict]) -> str:
    kind = (by_id.get(result_id) or {}).get("kind")
    label = _KIND_LABEL.get(kind or "")
    return f"{result_id} ({label})" if label else result_id


def _measures_frequency(record: dict) -> bool:
    """Whether a cited result can back a frequency claim.

    Counts and trends do; so do derived results computed from them (the priority
    ranking). A derived result that says otherwise (the timeline: rows around one
    line) does not.
    """
    if record.get("measures_frequency") is False:
        return False
    return record.get("kind") in MEASURED_KINDS | {"derived"}


_CLAIM_NOTES = {
    "relabel": " (the cited result does not address this claim)",
    "contradicted": " (the cited result contradicts this claim)",
}


def _findings_section(
    findings: list,
    valid_ids: set[str],
    by_id: dict,
    claim_verdicts: Mapping[int, dict] | None = None,
) -> str:
    groups: dict[str, list[str]] = {"measured": [], "hypothesis": [], "limitation": []}
    for position, finding in enumerate(findings):
        kind = _field(finding, "kind") or "hypothesis"
        statement = _clean(_field(finding, "statement"))
        if not statement:
            continue
        fid = _field(finding, "id") or ""
        cited = [rid for rid in _field(finding, "result_ids") or [] if rid in valid_ids]
        if kind == "measured" and not cited:
            # A "fact" with no valid evidence is not a measured fact.
            kind = "hypothesis"
            statement += " (not backed by a cited result)"
        elif kind == "measured" and by_id:
            # Every figure in a measured claim must be in the results it cites.
            # A sum, share or rounding the model made up is not measured.
            missing = unsupported_numbers(statement, [by_id[r] for r in cited if r in by_id])
            if missing:
                logger.info("measured finding %s downgraded: %s", fid, missing)
                kind = "hypothesis"
                statement += (
                    " (not shown by the cited result: " + ", ".join(missing) + ")"
                )
        if kind == "measured" and position in (claim_verdicts or {}):
            # The semantic check found the cited results do not back the claim.
            verdict = claim_verdicts[position]
            kind = "hypothesis"
            statement += _CLAIM_NOTES.get(verdict.get("action"), "")
        line = f"- **{fid}** {statement}" if fid else f"- {statement}"
        if cited:
            line += " — " + ", ".join(_citation(rid, by_id) for rid in cited)
            if kind == "measured" and not any(
                _measures_frequency(by_id.get(rid) or {}) for rid in cited
            ) and by_id:
                line += " (row-level evidence, not a frequency)"
        groups.get(kind, groups["hypothesis"]).append(line)

    sections = [
        f"{FINDING_TITLES[kind]}\n" + "\n".join(lines)
        for kind, lines in groups.items()
        if lines
    ]
    return "\n\n".join(sections)


def _recommendations_section(recommendations: list) -> str:
    lines = []
    for index, rec in enumerate(recommendations, start=1):
        action = _clean(_field(rec, "action"))
        if not action:
            continue
        line = f"{index}. **{action}**"
        why = _clean(_field(rec, "why"))
        if why:
            line += f" — {why}"
        extras = []
        if _field(rec, "owner_team"):
            extras.append(f"owner: {_field(rec, 'owner_team')}")
        if _field(rec, "finding_ids"):
            extras.append("based on " + ", ".join(_field(rec, "finding_ids")))
        if extras:
            line += f" ({'; '.join(extras)})"
        lines.append(line)
    return f"{RECOMMENDATIONS_TITLE}\n" + "\n".join(lines) if lines else ""


def answer_layout() -> str:
    """The sections of an answer, for an agent that has no code to build them.

    The chatbot's answer node builds these sections in code from the cited query
    results. An external agent writing its own answer is asked for the same
    layout, and for the same rule that keeps it honest: a statement is only
    "measured" when a query that actually ran backs it.
    """
    return (
        "Write the answer, then finish with these sections:\n"
        f"{FINDING_TITLES['measured']}: a statement belongs here only if a query "
        "you ran shows it; cite that query. Anything else is a hypothesis.\n"
        f"{FINDING_TITLES['hypothesis']}: explanations the logs do not confirm.\n"
        f"{FINDING_TITLES['limitation']}: what remains unknown.\n"
        f"{RECOMMENDATIONS_TITLE}: each action with its reason and, where the "
        "ownership mapping names one, the owning team.\n"
        f"{APPENDIX_HEADING}: the datasets, time windows and filters used; which "
        "results are measured counts and which are examples; whether each result "
        "was truncated; and the executed query for every result."
    )


# --- data-scope appendix ---------------------------------------------------


def _fmt(value: Any) -> str:
    return f'"{value}"' if isinstance(value, str) else str(value)


def _truncation(record: dict) -> str:
    truncated = record.get("truncated")
    total = record.get("total_matches")
    returned = record.get("returned") or record.get("row_count")
    pool = record.get("total_matches_any")
    if pool is not None and pool != total:
        # A search whose words do not all appear together: the rows are the best
        # of the any-word pool, so the all-words total is not the denominator.
        mode = record.get("search_mode")
        suffix = f"; {mode} search" if mode else ""
        return (
            f"{'truncated: yes' if truncated else 'truncated: no'} "
            f"({returned} rows returned of {pool} matching any search word; "
            f"{total} contain every word{suffix})"
        )
    if truncated is True:
        more = f" of {total}" if total is not None else ""
        return f"truncated: yes ({returned}{more} rows returned; more matching rows exist)"
    if truncated is False:
        # A counted total is worth stating even when nothing was cut: "3 of 3"
        # says these are all the matches, which "truncated: no" alone leaves
        # for the reader to infer.
        if total is not None:
            return f"truncated: no ({returned or 0} of {total} matching)"
        return "truncated: no"
    return "truncated: unknown (not reported by this tool)"


def render_appendix(records: Sequence[dict], notes: Sequence[str] = ()) -> str:
    """The code-built "Data scope and queries" section for these records."""
    records = [r for r in records if isinstance(r, dict)]
    scope = build_data_scope(records)
    lines = [APPENDIX_HEADING]
    lines.append(
        "- Datasets: " + (", ".join(scope.datasets) if scope.datasets else "not reported")
    )
    if scope.time_windows:
        windows = [
            f"{w.get('label') + ' ' if w.get('label') else ''}{w['start']} – {w['end']}"
            for w in scope.time_windows
        ]
        lines.append("- Time window: " + "; ".join(windows))
    else:
        lines.append("- Time window: none applied (whole table)")
    if scope.filters:
        lines.append(
            "- Filters: "
            + "; ".join(
                f"{key}=" + " | ".join(_fmt(v) for v in values)
                for key, values in scope.filters.items()
            )
        )
    if scope.measured_result_ids:
        lines.append("- Measured counts/trends: " + ", ".join(scope.measured_result_ids))
    if scope.example_result_ids:
        lines.append(
            "- Examples only (do not measure frequency): "
            + ", ".join(scope.example_result_ids)
        )
    if scope.truncated_result_ids:
        lines.append("- Truncated: " + ", ".join(scope.truncated_result_ids))
    for note in notes:
        lines.append(f"- Note: {note}")

    for record in records:
        kind = record.get("kind") or "unknown"
        header = (
            f"- `{record.get('id')}` {record.get('tool_name')} — {kind}, "
            f"{record.get('row_count')} rows; {_truncation(record)}"
        )
        if record.get("rows_capped"):
            header += f"; table shows the first {len(record.get('rows') or [])}"
        lines.append(header)
        if kind == "derived":
            sources = ", ".join(record.get("source_ids") or [])
            line = f"  computed in code from {sources}: {record.get('formula')}"
            if record.get("approximate_datasets"):
                line += (
                    "; approximate: "
                    + ", ".join(record["approximate_datasets"])
                )
            lines.append(line)
            continue
        query = record.get("query")
        if query:
            if len(query) > MAX_QUERY_CHARS:
                query = query[:MAX_QUERY_CHARS] + "\n-- (query shortened for display)"
            lines.append("```sql\n" + query + "\n```")
        else:
            lines.append(
                f"  query: not reported by this tool; arguments {record.get('arguments')}"
            )
    return "\n".join(lines)


# --- tolerant readers ------------------------------------------------------


def _read(answer: Any, name: str) -> list:
    """Return a list field from a pydantic ``AnswerOutput`` or a plain dict."""
    if isinstance(answer, dict):
        return answer.get(name) or []
    return getattr(answer, name, None) or []


def _field(item: Any, name: str) -> Any:
    if isinstance(item, dict):
        return item.get(name)
    return getattr(item, name, None)


def _read_block(block: Any) -> tuple[str, str, str]:
    """Return ``(kind, text, result_id)`` for a pydantic or dict block."""
    if isinstance(block, AnswerTable):
        return "table", "", block.result_id
    if isinstance(block, AnswerText):
        return "text", block.text, ""
    if isinstance(block, dict):
        if block.get("type") == "table" or block.get("result_id"):
            return "table", "", block.get("result_id", "")
        return "text", block.get("text", ""), ""
    if getattr(block, "result_id", None):
        return "table", "", block.result_id
    return "text", getattr(block, "text", ""), ""
