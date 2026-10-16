"""Rendering helpers for assistant answers that embed [[RESULT:Rx]] markers.
Markers are normally well formed on their own line, but sloppy variants (inline,
wrapped, bold, quotes, CJK brackets) are tolerated and canonicalized in order.
"""

import re

import pandas as pd

# Canonical marker is [[RESULT:R1]], but models sometimes emit variants such as
# [RESULT:R1], 【RESULT:R1】, ［RESULT:R1］, or 【R1】 with CJK/fullwidth brackets.
# Accept all of them so a table still renders and no raw marker leaks into the UI.
MARKER_RE = re.compile(r"[\[【［]{1,2}\s*(?:RESULT\s*:\s*)?(R\d+)\s*[\]】］]{1,2}")

# Balanced wrapper pairs that decorate a marker rather than belong to the
# sentence (e.g. "([[RESULT:R1]])" or "**[[RESULT:R1]]**").
_WRAPPER_PAIRS = {
    "(": ")",
    "[": "]",
    "{": "}",
    "（": "）",
    "［": "］",
    "【": "】",
    "「": "」",
    "『": "』",
    "\u201c": "\u201d",
    "\u2018": "\u2019",
    '"': '"',
    "'": "'",
    "*": "*",
    "_": "_",
    "`": "`",
}

# Characters ignored when deciding whether a line contains nothing but markers:
# wrappers, emphasis, quotes, and list/clause punctuation. A line that reduces to
# nothing is a "table line" (the table renders in place there).
_LINE_DECORATION = " \t\r()[]{}<>（）［］【】「」『』\"'“”‘’*_`~#=—–-·•:;,.!?|/\\"


def canonicalize(text: str) -> list[tuple[str, str]]:
    """Split an answer into ordered ``("text" | "table", value)`` blocks.
    Marker-only lines become table blocks in place; inline markers are stripped
    (with wrappers), their tables emitted after; unknown handling is the caller's."""
    blocks: list[tuple[str, str]] = []
    text_lines: list[str] = []
    inline_tables: list[str] = []

    def flush() -> None:
        prose = "\n".join(text_lines).strip("\n")
        if prose.strip():
            blocks.append(("text", prose))
        blocks.extend(("table", result_id) for result_id in inline_tables)
        text_lines.clear()
        inline_tables.clear()

    for raw_line in text.split("\n"):
        if not raw_line.strip():
            flush()
            continue

        matches = list(MARKER_RE.finditer(raw_line))
        if not matches:
            text_lines.append(raw_line)
            continue

        if _is_table_line(raw_line):
            flush()
            blocks.extend(("table", match.group(1)) for match in matches)
        else:
            cleaned, result_ids = _strip_inline_markers(raw_line)
            text_lines.append(cleaned)
            inline_tables.extend(result_ids)

    flush()
    return blocks


def render_answer(container, text: str, results: list | None) -> None:
    """Render prose, swap each result marker for its data table, show the cost."""
    by_id = {r["id"]: r for r in (results or [])}
    rendered: set[str] = set()

    for kind, value in canonicalize(text):
        if kind == "text":
            if value.strip():
                container.markdown(value)
            continue
        # Unknown id → render nothing (guards against hallucinated markers).
        if value in rendered or value not in by_id:
            continue
        rows = by_id[value].get("rows")
        if not rows:
            continue
        rendered.add(value)
        container.dataframe(
            pd.DataFrame(rows),
            width="stretch",
            hide_index=True,
        )

    # Trace before cost: how the data was reached, then how much it cost.
    render_sweep_trace(container, results)
    render_cost_panel(container, results)


def cost_rows(results: list | None) -> list[dict]:
    """Per-query cost rows for the panel, derived from each result's stats.

    Pure, so the table's contents are testable without Streamlit. Returns an
    empty list when no tool reported cost, which is the signal to show nothing.
    """
    rows: list[dict] = []
    for record in results or []:
        stats = record.get("query_stats")
        if not isinstance(stats, dict):
            continue
        rows_scanned = int(stats.get("est_rows_scanned") or 0)
        rows.append(
            {
                "Result": record.get("id"),
                "Dataset": stats.get("dataset") or "-",
                "Rows out": int(stats.get("row_count") or record.get("row_count") or 0),
                "Rows scanned": f"{rows_scanned:,}" if rows_scanned else "-",
                "Read": _human_bytes(int(stats.get("est_bytes_read") or 0)),
                "Time": f"{int(stats.get('elapsed_ms') or 0)} ms",
                # Badged rather than hidden: an approximate aggregate must never
                # be mistaken for an exact count.
                "Exact": "~approx" if stats.get("approximate") else "exact",
            }
        )
    return rows


def cost_total(results: list | None) -> str:
    """One-line total for the panel caption, or an empty string."""
    rows = [
        r.get("query_stats")
        for r in (results or [])
        if isinstance(r.get("query_stats"), dict)
    ]
    if not rows:
        return ""

    scanned = sum(int(s.get("est_rows_scanned") or 0) for s in rows)
    read = sum(int(s.get("est_bytes_read") or 0) for s in rows)
    elapsed = sum(int(s.get("elapsed_ms") or 0) for s in rows)
    approximate = sum(1 for s in rows if s.get("approximate"))

    parts = [f"{len(rows)} queries", f"{elapsed / 1000:.1f}s"]
    if scanned:
        parts.insert(1, f"~{scanned:,} rows scanned")
    if read:
        parts.insert(2, _human_bytes(read))
    if approximate:
        parts.append(f"{approximate} approximate")
    return " · ".join(parts)


def render_cost_panel(container, results: list | None) -> None:
    """Show what the investigation actually read, in a collapsed expander.

    Collapsed because it is supporting detail, present because the product
    promises to report data volume, timings, and whether a figure is exact.
    """
    rows = cost_rows(results)
    if not rows:
        return

    # Write through the expander object rather than the streamlit module, so this
    # stays consistent with how the rest of the renderer takes its container.
    panel = container.expander(
        f"Data queried — {cost_total(results)}", expanded=False
    )
    panel.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)


# Stage labels for the sweep trace, in the order the stages run.
_STAGE_LABELS = {
    "map": "Mapped",
    "profile": "Profiled",
    "sample": "Sampled",
    "decide": "Decided",
    "promote": "Read in full",
    "done": "Finished",
}


def sweep_trace_rows(results: list | None) -> list[dict]:
    """Flatten any sweep traces on the results into display rows.

    The sweep's work happens inside one tool call, so its result arrives with no
    visible account of how it was reached. The trace is that account: which pass
    examined what, what each chunk scored, and why anything was read in full.
    """
    rows: list[dict] = []
    for result in results or []:
        if not isinstance(result, dict):
            continue
        for event in result.get("progress_trace") or []:
            if not isinstance(event, dict):
                continue
            stage = str(event.get("stage", ""))
            detail = event.get("detail") or {}
            rows.append(
                {
                    "Result": result.get("id", ""),
                    "Stage": _STAGE_LABELS.get(stage, stage or "-"),
                    "What happened": str(event.get("message", "")),
                    # Whether this step paid to read message text is the single
                    # most useful thing to see: it separates the cheap passes
                    # that covered everything from the expensive ones that did
                    # not.
                    "Read text": (
                        "yes" if detail.get("reads_message_text") else "no"
                    )
                    if "reads_message_text" in detail
                    else "-",
                    "Score": (
                        f"{float(detail['score']):.2f}"
                        if isinstance(detail.get("score"), (int, float))
                        else "-"
                    ),
                }
            )
    return rows


def render_sweep_trace(container, results: list | None) -> None:
    """Show how a corpus sweep reached its conclusions, collapsed by default.

    Present because the sweep is the one operation whose reasoning is otherwise
    invisible: a single tool call covering up to 1,624 chunks. Collapsed because
    most investigations never sweep, and when they do the answer's own coverage
    statement is the headline.
    """
    rows = sweep_trace_rows(results)
    if not rows:
        return

    read_steps = sum(1 for row in rows if row["Read text"] == "yes")
    panel = container.expander(
        f"How the corpus was swept — {len(rows)} steps, "
        f"{read_steps} that read message text",
        expanded=False,
    )
    panel.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)


def _human_bytes(num_bytes: int) -> str:
    if not num_bytes:
        return "-"
    size = float(num_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(size) < 1024.0 or unit == "TiB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TiB"  # pragma: no cover


def _is_table_line(line: str) -> bool:
    """Whether a line is nothing but markers plus decoration (wrappers, punct)."""
    return not MARKER_RE.sub("", line).strip(_LINE_DECORATION)


def _strip_inline_markers(line: str) -> tuple[str, list[str]]:
    """Remove inline markers (and their wrappers); return the cleaned line and ids."""
    parts: list[str] = []
    result_ids: list[str] = []
    pos = 0

    for match in MARKER_RE.finditer(line):
        start, end = _expand_wrappers(line, match.start(), match.end())
        parts.append(line[pos:start])
        result_ids.append(match.group(1))
        pos = end
    parts.append(line[pos:])

    cleaned = re.sub(r"[ \t]{2,}", " ", "".join(parts))
    cleaned = re.sub(r"[ \t]+([,.;:!?])", r"\1", cleaned)
    return cleaned, result_ids


def _expand_wrappers(line: str, start: int, end: int) -> tuple[int, int]:
    """Grow ``[start, end)`` over balanced wrapper pairs hugging the marker."""
    while True:
        if line[start - 2 : start] == "**" and line[end : end + 2] == "**":
            start, end = start - 2, end + 2
            continue
        if start > 0 and end < len(line):
            opener, closer = line[start - 1], line[end]
            if _WRAPPER_PAIRS.get(opener) == closer:
                start, end = start - 1, end + 1
                continue
        return start, end
