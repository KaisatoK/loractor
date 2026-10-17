"""Generate the worked investigation document from a live run.

One topic is put to the same pipeline as engineer, team lead and manager (the
same agent and base corpus); the document records, per role, the question, the
route, every tool call with its executed query and key rows, the evidence
report, the answer and its recommended action, and what the data does and does
not establish. It is regenerated from real runs, so it always matches the code.

Run it from the repo root against a live SereneDB and real provider keys::

    bash ./worked_investigation.sh --topic "<issue or pattern>"

``build_markdown`` is pure (no network, no database) and is covered offline by
``tests/test_worked_investigation.py``; only :func:`run_role` calls the pipeline.
Questions are per-role phrasings of one topic (override any with the
``--engineer-question`` / ``--team-lead-question`` / ``--manager-question``
options); pick the topic after looking at the real data, see AGENTS.md
"Integrating new tools and tables".
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.pipeline.answers import APPENDIX_HEADING, strip_appendix
from backend.pipeline.data_scope import build_data_scope
from backend.pipeline.tools.envelope import MEASURED_KINDS

ROLES = ("engineer", "team_lead", "manager")
ROLE_TITLES = {"engineer": "Engineer", "team_lead": "Team lead", "manager": "Manager"}

QUESTION_TEMPLATES = {
    "engineer": (
        "What failed involving {topic}, in which component, and what happened "
        "just before and after?"
    ),
    "team_lead": (
        "Which problems involving {topic} recur, how often, and which "
        "components or teams are involved?"
    ),
    "manager": (
        "Which observed problems involving {topic} deserve attention first, "
        "and what should happen next?"
    ),
}

DEFAULT_OUT = Path("docs") / "worked_investigation.md"
DEFAULT_RAW = Path(".data") / "worked_investigation_runs.json"
SAMPLE_ROWS = 5
MAX_CELL = 120
_MARKER_LINE = re.compile(r"^\s*\[\[RESULT:(R\d+)\]\]\s*$")


# --- markdown helpers (pure) ---------------------------------------------------


def _cell(value: Any) -> str:
    text = "" if value is None else str(value)
    text = text.replace("|", "\\|").replace("\n", " ").strip()
    return text if len(text) <= MAX_CELL else text[: MAX_CELL - 1] + "…"


def table(rows: Sequence[dict], limit: int = SAMPLE_ROWS) -> str:
    """A markdown table of the first ``limit`` rows (all columns of row 0)."""
    if not rows:
        return "_no rows_"
    columns = list(rows[0].keys())
    lines = [
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    for row in rows[:limit]:
        lines.append("| " + " | ".join(_cell(row.get(c)) for c in columns) + " |")
    return "\n".join(lines)


def answer_markdown(answer: str, records: Sequence[dict]) -> str:
    """The answer with each ``[[RESULT:Rn]]`` marker replaced by its table."""
    by_id = {r["id"]: r for r in records if isinstance(r, dict) and "id" in r}
    out = []
    for line in (answer or "").splitlines():
        match = _MARKER_LINE.match(line)
        if match and match.group(1) in by_id:
            record = by_id[match.group(1)]
            out.append(f"_{record['id']}: {record.get('tool_name')}_\n\n{table(record.get('rows') or [])}")
        elif match:
            continue
        else:
            out.append(line)
    return "\n".join(out)


def ledger(answer: str) -> dict[str, list[str]]:
    """Measured / hypothesis / limitation bullets parsed from a rendered answer."""
    sections = {"measured": [], "hypothesis": [], "limitation": []}
    current = None
    for line in strip_appendix(answer or "").splitlines():
        if line.startswith("**Measured**"):
            current = "measured"
        elif line.startswith("**Hypotheses**"):
            current = "hypothesis"
        elif line.startswith("**Limitations**"):
            current = "limitation"
        elif line.startswith("**Recommended"):
            current = None
        elif current and line.startswith("- "):
            sections[current].append(line[2:])
    return sections


def recommendations(answer: str) -> list[str]:
    """The numbered recommendation lines of a rendered answer."""
    lines, inside = [], False
    for line in strip_appendix(answer or "").splitlines():
        if line.startswith("**Recommended"):
            inside = True
        elif inside and re.match(r"^\d+\. ", line):
            lines.append(line)
        elif inside and line.strip() and not re.match(r"^\d+\. ", line):
            inside = False
    return lines


def coverage(records: Sequence[dict]) -> dict[str, bool]:
    """Whether a run used full-text search and a whole-scope aggregation."""
    kinds = {r.get("kind") for r in records if isinstance(r, dict)}
    return {"search": "search" in kinds, "aggregation": bool(kinds & MEASURED_KINDS)}


# --- document ---------------------------------------------------------------------


def _tool_calls(records: Sequence[dict]) -> str:
    if not records:
        return "_No tool results were captured._"
    lines = ["| id | tool | kind | arguments | rows | truncated |", "| --- | --- | --- | --- | --- | --- |"]
    for r in records:
        truncated = {True: "yes", False: "no", None: "unknown"}[r.get("truncated")]
        lines.append(
            f"| {r['id']} | {r.get('tool_name')} | {r.get('kind') or 'unknown'} | "
            f"`{_cell(json.dumps(r.get('arguments'), default=str))}` | "
            f"{r.get('row_count')} | {truncated} |"
        )
    return "\n".join(lines)


def _queries(records: Sequence[dict]) -> str:
    parts = []
    for r in records:
        if r.get("kind") == "derived":
            parts.append(f"**{r['id']}** computed in code from {', '.join(r.get('source_ids') or [])}: {r.get('formula')}")
        elif r.get("query"):
            parts.append(f"**{r['id']}** {r.get('tool_name')}\n\n```sql\n{r['query']}\n```")
        else:
            parts.append(f"**{r['id']}** {r.get('tool_name')}: query not reported by this tool.")
    return "\n\n".join(parts) or "_none_"


def _key_results(records: Sequence[dict]) -> str:
    parts = []
    for r in records:
        shown = min(SAMPLE_ROWS, len(r.get("rows") or []))
        parts.append(
            f"**{r['id']}** ({r.get('kind') or 'unknown'}, first {shown} of {r.get('row_count')} rows)\n\n"
            + table(r.get("rows") or [])
        )
    return "\n\n".join(parts) or "_none_"


def _evidence(report: dict | None) -> str:
    if not report:
        return "_No evidence report (no investigation ran for this route)._"
    lines = [f"**Summary:** {report.get('summary') or '—'}"]
    for item in report.get("evidence") or []:
        lines.append(
            f"- **{item.get('id')}** {item.get('name')} — {item.get('availability')}; "
            f"results {', '.join(item.get('result_ids') or []) or 'none'}"
        )
    for h in report.get("hypotheses") or []:
        line = f"- Hypothesis ({h.get('status')}): {h.get('hypothesis')}"
        if h.get("contradicting_evidence_ids"):
            line += f"; contradicted by {', '.join(h['contradicting_evidence_ids'])}"
        if h.get("next_check"):
            line += f"; next check: {h['next_check']}"
        lines.append(line)
    return "\n".join(lines)


def _role_section(run: dict) -> str:
    role = run["role"]
    records = run.get("tool_results") or []
    answer = run.get("answer") or ""
    cov = coverage(records)
    scope = build_data_scope(records)
    notes = []
    if run.get("scope") not in (None, "find_evidence"):
        notes.append(
            f"> Route was `{run['scope']}`, not an investigation; the answer below is the "
            "assistant's reply, with no evidence behind it."
        )
    if run.get("error"):
        notes.append(f"> Run failed: `{run['error']}`")
    elif not cov["search"] and not cov["aggregation"] and run.get("scope") == "find_evidence":
        notes.append("> ⚠ This run captured no search or aggregation results.")
    elif role == "engineer" and not cov["search"]:
        notes.append("> ⚠ No full-text search result was captured for this role.")
    elif role != "engineer" and not cov["aggregation"]:
        notes.append("> ⚠ No whole-scope aggregation was captured for this role.")

    lines = [
        f"## {ROLE_TITLES[role]}",
        f"**Question:** {run['question']}",
        "",
        f"**Route:** `{run.get('scope')}` · **Effort:** {run.get('effort')} · "
        f"**Datasets:** {', '.join(scope.datasets) or 'not reported'}",
        *notes,
        "",
        "### Tool calls",
        _tool_calls(records),
        "",
        "### Executed queries",
        _queries(records),
        "",
        "### Relevant results",
        _key_results(records),
        "",
        "### Evidence report",
        _evidence(run.get("evidence_report")),
        "",
        f"### Answer for the {ROLE_TITLES[role].lower()}",
        answer_markdown(strip_appendix(answer), records) if answer else "_no answer_",
        "",
    ]
    recs = recommendations(answer)
    lines += ["### Recommended action", *(recs or ["_none recorded_"]), ""]
    return "\n".join(lines)


def _establishes(runs: Sequence[dict]) -> str:
    out = ["## What the data does and does not establish", ""]
    out.append("**Established (measured, cited to query results):**")
    measured = [(r["role"], m) for r in runs for m in ledger(r.get("answer") or "")["measured"]]
    out += [f"- ({ROLE_TITLES[role]}) {m}" for role, m in measured] or ["- _none recorded_"]
    out += ["", "**Not established (hypotheses and limitations):**"]
    open_items = [
        (r["role"], h)
        for r in runs
        for key in ("hypothesis", "limitation")
        for h in ledger(r.get("answer") or "")[key]
    ]
    out += [f"- ({ROLE_TITLES[role]}) {h}" for role, h in open_items] or ["- _none recorded_"]
    out += [
        "",
        "General limits that apply to every role: log entries are counted, not "
        "incidents; retrieved examples show what a message looks like but never how "
        "often it happens; the logs contain no cost, downtime or customer-impact "
        "data, so none is claimed; team ownership comes from a fictional mapping.",
    ]
    return "\n".join(out)


def build_markdown(runs: Sequence[dict], *, topic: str, generated: str | None = None) -> str:
    """The worked-investigation document for these role runs."""
    generated = generated or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    all_records = [rec for run in runs for rec in run.get("tool_results") or []]
    scope = build_data_scope(all_records)
    head = [
        "# Worked investigation",
        "",
        f"Topic: **{topic}**. The same agent and base corpus answer three role-specific "
        "questions; each role gets different evidence and a different next step.",
        "",
        f"_Generated {generated} by `bash ./worked_investigation.sh`; this file is "
        "regenerated from live runs, do not edit by hand._",
        "",
        "## Data scope",
        f"- Datasets queried: {', '.join(scope.datasets) or 'not reported'}",
        "- Time windows: "
        + ("; ".join(f"{w['start']} – {w['end']}" for w in scope.time_windows) or "none applied (whole table)"),
        f"- Roles run: {', '.join(ROLE_TITLES[r['role']] for r in runs)}",
        "",
    ]
    body = [_role_section(run) for run in runs]
    return "\n".join(head) + "\n" + "\n".join(body) + "\n" + _establishes(runs) + "\n"


# --- live runner -------------------------------------------------------------------


def run_role(role: str, question: str, effort: str) -> dict[str, Any]:
    """Run the real pipeline for one role on its own thread; return the record."""
    from backend.pipeline.graph import chatbot
    from backend.pipeline.roles import ROLE_OBJECTIVES

    state = {
        "conversation": f"user: {question}",
        "messages": [{"role": "user", "content": question}],
        "latest_message": question,
        "latest_answer": "",
        "role": {"name": role, "objective": ROLE_OBJECTIVES[role]},
        "effort": effort,
        "answer": "",
    }
    record: dict[str, Any] = {"role": role, "question": question, "effort": effort}
    try:
        final = chatbot.invoke(
            state, config={"configurable": {"thread_id": f"worked:{role}:{uuid.uuid4().hex}"}}
        )
    except Exception as exc:  # noqa: BLE001 - record the failure in the document
        record["error"] = f"{type(exc).__name__}: {exc}"
        return record
    record.update(
        scope=final.get("scope"),
        answer=final.get("answer", ""),
        tool_results=final.get("tool_results") or [],
        evidence_report=final.get("evidence_report"),
    )
    return record


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--topic", required=True, help="the issue or pattern to investigate")
    parser.add_argument("--effort", default="High", choices=["Low", "Medium", "High", "Extreme", "Brutal"])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--raw", type=Path, default=DEFAULT_RAW, help="where to dump the raw run records")
    for role in ROLES:
        parser.add_argument(f"--{role.replace('_', '-')}-question", dest=f"{role}_question")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    os.environ.setdefault("CHECKPOINT_BACKEND", "memory")  # do not touch .data/checkpoints
    runs = []
    for role in ROLES:
        question = getattr(args, f"{role}_question") or QUESTION_TEMPLATES[role].format(topic=args.topic)
        print(f"[{role}] {question}", flush=True)
        runs.append(run_role(role, question, args.effort))
        print(f"[{role}] route={runs[-1].get('scope')} error={runs[-1].get('error')}", flush=True)
    args.raw.parent.mkdir(parents=True, exist_ok=True)
    args.raw.write_text(json.dumps(runs, indent=2, default=str), encoding="utf-8")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(build_markdown(runs, topic=args.topic), encoding="utf-8")
    print(f"Wrote {args.out} (raw runs: {args.raw})")
    return 1 if any(r.get("error") for r in runs) else 0


if __name__ == "__main__":
    sys.exit(main())
