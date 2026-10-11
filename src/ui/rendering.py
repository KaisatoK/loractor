"""Rendering helpers for assistant answers that embed [[RESULT:Rx]] markers.

The pipeline serializes a structured answer into the marker protocol, so markers
are normally well formed and alone on their own line. The renderer still tolerates
the sloppy variants a model can produce — inline markers, wrapped in parentheses,
bold, quotes, or CJK/fullwidth brackets — and canonicalizes them: the prose is
cleaned of marker decoration and each table is placed in reading order (an inline
marker's table follows its paragraph).
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

    Marker-only lines become table blocks in place; markers embedded in prose are
    removed (with their wrapper punctuation) and their tables are emitted after
    the surrounding paragraph. Unknown handling (missing ids, dedupe) is left to
    the caller.
    """
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
    """Render prose and swap each result marker for its data table."""
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
