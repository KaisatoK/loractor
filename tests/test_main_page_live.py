"""Tests for the UI's live-run buffer (poll into session state, redraw to view).

The live view must report the *current* cycle's progress without stacking it up,
so these lock in the contract: events are folded into a buffer without rendering,
a new stage drops the previous cycle, and each render pass redraws the whole
current cycle into an ``st.empty`` box (replace, not accumulate).
"""

from ui.main_page.main_page import (
    _apply_event,
    _new_run,
    _render_live,
)


class FakeContainer:
    """Records what a render pass drew, in order."""

    def __init__(self):
        self.drawn = []

    def caption(self, text):
        self.drawn.append(("caption", text))

    def markdown(self, text):
        self.drawn.append(("markdown", text))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeBox:
    """Stands in for ``st.empty()``: each ``container()`` starts a fresh draw."""

    def __init__(self):
        self.runs = []

    def container(self):
        container = FakeContainer()
        self.runs.append(container)
        return container

    @property
    def last(self):
        return self.runs[-1]


def test_new_run_buffers_a_placeholder_and_starts_unshown():
    run = _new_run("r1", "why?")
    assert run["run_id"] == "r1"
    assert run["status"] == "running"
    assert run["cycle_start"] == 0
    assert run["log"] == [{"kind": "progress", "text": "Investigating…"}]


def test_apply_event_buffers_progress_without_rendering():
    run = _new_run("r1", "why?")
    _apply_event(run, {"type": "subgraph_message", "content": "reasoning"})
    _apply_event(
        run,
        {
            "type": "tool_progress",
            "content": '{"operation": "search", "status": "completed"}',
        },
    )

    assert run["progress"] == ["Thinking: reasoning", "✓ search"]
    assert [item["text"] for item in run["log"][1:]] == run["progress"]


def test_new_stage_drops_the_previous_cycle_from_the_live_view():
    run = _new_run("r1", "why?")
    _apply_event(run, {"type": "subgraph_message", "content": "old thinking"})
    _apply_event(run, {"type": "subgraph_name", "content": "find_evidence"})
    _apply_event(run, {"type": "subgraph_message", "content": "new thinking"})

    # The full progress is still kept for persistence...
    assert run["progress"] == [
        "Thinking: old thinking",
        "Currently at stage: find_evidence",
        "Thinking: new thinking",
    ]
    # ...but the live view starts at the newest stage.
    box = FakeBox()
    _render_live(box, run)
    assert box.last.drawn == [
        ("caption", "Currently at stage: find_evidence"),
        ("caption", "Thinking: new thinking"),
    ]


def test_render_redraws_the_whole_current_cycle_each_tick():
    run = _new_run("r1", "why?")
    _apply_event(run, {"type": "subgraph_message", "content": "step one"})
    box = FakeBox()
    _render_live(box, run)
    assert box.last.drawn == [
        ("caption", "Investigating…"),
        ("caption", "Thinking: step one"),
    ]

    # A later tick with one more line redraws both, so the older line is not lost.
    _apply_event(run, {"type": "subgraph_message", "content": "step two"})
    _render_live(box, run)
    assert [text for _, text in box.last.drawn] == [
        "Investigating…",
        "Thinking: step one",
        "Thinking: step two",
    ]


def test_answer_is_rendered_as_markdown_with_marker_swapped():
    run = _new_run("r1", "why?")
    _apply_event(run, {"type": "messages", "content": "Done [[RESULT:R1]]"})
    _apply_event(run, {"type": "messages", "content": ""})  # ignored, no empty item
    box = FakeBox()
    _render_live(box, run)

    assert box.last.drawn[-1] == ("markdown", "Done _loading table…_")
    assert run["answer"] == ""
