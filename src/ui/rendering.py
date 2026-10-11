"""Rendering helpers for assistant answers that embed [[RESULT:Rx]] markers."""

import re

import pandas as pd

# Canonical marker is [[RESULT:R1]], but models sometimes emit variants such as
# [RESULT:R1], 【RESULT:R1】, ［RESULT:R1］, or 【R1】 with CJK/fullwidth brackets.
# Accept all of them so a table still renders and no raw marker leaks into the UI.
MARKER_RE = re.compile(r"[\[【［]{1,2}\s*(?:RESULT\s*:\s*)?(R\d+)\s*[\]】］]{1,2}")
# Sentence/clause punctuation that belongs to the text preceding a marker.
TRAILING_PUNCT = ".,;:!?"


def render_answer(container, text: str, results: list | None) -> None:
    """Render prose and swap each result marker for its data table."""
    by_id = {r["id"]: r for r in (results or [])}
    pos = 0
    for match in MARKER_RE.finditer(text):
        # Punctuation right after a marker (e.g. "[[RESULT:R1]].") belongs to the
        # preceding sentence. Move it before the marker so it does not render on
        # its own line underneath the table.
        end = match.end()
        trailing = ""
        while end < len(text) and text[end] in TRAILING_PUNCT:
            trailing += text[end]
            end += 1
        chunk = text[pos : match.start()].rstrip() + trailing
        if chunk.strip():
            container.markdown(chunk)
        record = by_id.get(match.group(1))
        if record and record.get("rows"):
            container.dataframe(
                pd.DataFrame(record["rows"]),
                width="stretch",
                hide_index=True,
            )
        # unknown id → render nothing (guards against hallucinated markers)
        pos = end
    tail = text[pos:]
    if tail.strip():
        container.markdown(tail)
