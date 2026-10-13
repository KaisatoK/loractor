"""Unit tests for answer serialization and marker rendering.

Pure/offline: these never touch SereneDB, unlike ``test_db_queries.py``. They
cover the structured-answer serializer and the markdown canonicalization that
guarantees no marker decoration ("weird trailing characters") reaches the UI.
"""

from backend.pipeline.answers import serialize_answer
from backend.pipeline.contracts import AnswerOutput, AnswerText
from ui.rendering import canonicalize, render_answer


class FakeContainer:
    """Captures the markdown/dataframe calls ``render_answer`` makes."""

    def __init__(self):
        self.calls = []

    def markdown(self, text):
        self.calls.append(("markdown", text))

    def dataframe(self, frame, **kwargs):
        self.calls.append(("dataframe", frame, kwargs))


def _results(*ids):
    return [{"id": result_id, "rows": [{"col": result_id}]} for result_id in ids]


def _kinds(container):
    return [kind for kind, *_ in container.calls]


def _prose(container):
    return [call[1] for call in container.calls if call[0] == "markdown"]


def _tables(container):
    return [call[1].iloc[0, 0] for call in container.calls if call[0] == "dataframe"]


# The reported failure: markers cited inline inside parentheses mid-sentence.
EXAMPLE = (
    "Effort & Confidence: High-confidence data from the top-service query "
    "([[RESULT:R1]]) and minute-over-minute comparison ([[RESULT:R3]]) require "
    "minimal investigation; remediation (e.g., log level tuning, performance "
    "profiling) is low-to-moderate effort."
)


def test_inline_wrapped_markers_leave_clean_prose():
    container = FakeContainer()
    render_answer(container, EXAMPLE, _results("R1", "R3"))

    # One intact paragraph, no dangling "(" / stray ")" or raw markers.
    assert _kinds(container) == ["markdown", "dataframe", "dataframe"]
    prose = _prose(container)[0]
    assert "([[" not in prose and "]]" not in prose
    assert "query and minute-over-minute comparison require" in prose
    assert prose.startswith("Effort & Confidence:")
    assert prose.endswith("low-to-moderate effort.")
    # Tables follow the paragraph, in first-reference order.
    assert _tables(container) == ["R1", "R3"]


def test_block_marker_renders_table_in_place():
    container = FakeContainer()
    render_answer(container, "Before.\n\n[[RESULT:R1]]\n\nAfter.", _results("R1"))

    assert _kinds(container) == ["markdown", "dataframe", "markdown"]
    assert _prose(container) == ["Before.", "After."]
    assert _tables(container) == ["R1"]


def test_bold_wrapped_block_marker():
    container = FakeContainer()
    render_answer(container, "**[[RESULT:R1]]**", _results("R1"))

    assert _kinds(container) == ["dataframe"]


def test_marker_with_trailing_period_is_in_place():
    container = FakeContainer()
    render_answer(container, "Intro\n[[RESULT:R1]].", _results("R1"))

    assert _kinds(container) == ["markdown", "dataframe"]
    assert _prose(container) == ["Intro"]


def test_cjk_marker_variant():
    container = FakeContainer()
    render_answer(container, "【RESULT:R1】", _results("R1"))

    assert _kinds(container) == ["dataframe"]


def test_short_form_marker_keeps_sentence_punctuation():
    container = FakeContainer()
    render_answer(container, "See [R1].", _results("R1"))

    assert _tables(container) == ["R1"]
    assert _prose(container) == ["See."]


def test_unknown_marker_renders_nothing():
    container = FakeContainer()
    render_answer(container, "Text\n\n[[RESULT:R9]]", _results("R1"))

    assert _kinds(container) == ["markdown"]
    assert _prose(container) == ["Text"]


def test_duplicate_markers_render_one_table():
    container = FakeContainer()
    render_answer(container, "[[RESULT:R1]]\n\n[[RESULT:R1]]", _results("R1"))

    assert _tables(container) == ["R1"]


def test_canonicalize_orders_inline_tables_after_paragraph():
    blocks = canonicalize("A ([[RESULT:R1]]) b.\n\n[[RESULT:R2]]\nc")
    assert blocks == [
        ("text", "A b."),
        ("table", "R1"),
        ("table", "R2"),
        ("text", "c"),
    ]


def _answer(blocks):
    return AnswerOutput.model_validate({"blocks": blocks})


def test_serialize_builds_canonical_markers():
    answer = _answer(
        [
            {"type": "text", "text": "Intro."},
            {"type": "table", "result_id": "R1"},
            {"type": "text", "text": "After."},
        ]
    )
    assert serialize_answer(answer, {"R1"}) == "Intro.\n\n[[RESULT:R1]]\n\nAfter."


def test_serialize_drops_unknown_and_duplicate_ids():
    answer = _answer(
        [
            {"type": "table", "result_id": "R9"},
            {"type": "table", "result_id": "R1"},
            {"type": "table", "result_id": "R1"},
        ]
    )
    assert serialize_answer(answer, {"R1"}) == "[[RESULT:R1]]"


def test_serialize_keeps_all_valid_tables():
    answer = _answer([{"type": "table", "result_id": f"R{i}"} for i in range(1, 6)])
    serialized = serialize_answer(answer, {f"R{i}" for i in range(1, 6)})
    assert serialized.count("[[RESULT:") == 5


def test_serialize_strips_markers_from_prose():
    answer = _answer([{"type": "text", "text": "See [[RESULT:R1]] below."}])
    assert serialize_answer(answer, {"R1"}) == "See below."


def test_serialize_fallback_text_block():
    answer = AnswerOutput.model_construct(
        blocks=[AnswerText.model_construct(type="text", text="boom")]
    )
    assert serialize_answer(answer, set()) == "boom"


def test_serialize_empty_returns_empty():
    assert serialize_answer(AnswerOutput.model_construct(blocks=[]), set()) == ""
