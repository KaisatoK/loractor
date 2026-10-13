"""Tests for answer/marker rendering (pure, no Streamlit runtime)."""

import pandas as pd

from ui.rendering import MARKER_RE, render_answer


class RecordingContainer:
    def __init__(self):
        self.calls = []

    def markdown(self, text):
        self.calls.append(("markdown", text))

    def dataframe(self, df, **kwargs):
        self.calls.append(("dataframe", df))


def test_marker_regex_accepts_variants():
    for text in ["[[RESULT:R1]]", "[RESULT:R1]", "【RESULT:R1】", "［RESULT:R1］", "【R1】"]:
        assert MARKER_RE.search(text), text


def test_marker_swapped_for_table():
    container = RecordingContainer()
    render_answer(container, "before [[RESULT:R1]] after", [{"id": "R1", "rows": [{"a": 1}]}])
    kinds = [kind for kind, _ in container.calls]
    assert kinds == ["markdown", "dataframe", "markdown"]
    assert container.calls[1][1].equals(pd.DataFrame([{"a": 1}]))


def test_unknown_marker_renders_nothing():
    container = RecordingContainer()
    render_answer(container, "x [[RESULT:R9]] y", [])
    assert [kind for kind, _ in container.calls] == ["markdown", "markdown"]


def test_trailing_punctuation_moves_before_marker():
    container = RecordingContainer()
    render_answer(container, "done. [[RESULT:R1]]", [{"id": "R1", "rows": [{"a": 1}]}])
    assert container.calls[0][1] == "done."


def test_empty_marker_rows_are_skipped():
    container = RecordingContainer()
    render_answer(container, "only text [[RESULT:R1]]", [{"id": "R1", "rows": []}])
    assert [kind for kind, _ in container.calls] == ["markdown"]
