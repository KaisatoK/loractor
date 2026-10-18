"""The timeline diagram: DOT built in code from the derived ``timeline`` record."""

import shutil
import subprocess

import pandas as pd
import pytest

from backend.pipeline.timeline import timeline_record
from ui.rendering import render_answer
from ui.timeline_graph import MAX_NODES, timeline_dot


def _context_record():
    rows = [
        {"offset": o, "record_id": 100 + o, "ts": f"2017-05-16 00:00:{10 + o:02d}",
         "severity": "ERROR" if o == 0 else "INFO", "service": "nova.compute",
         "message": "boom", "request_id": "req-a" if o in (-1, 0) else "req-b"}
        for o in (-2, -1, 0, 1)
    ]
    return {"id": "R1", "tool_name": "get_nearby_events_tool", "kind": "context",
            "dataset": "openstack", "rows": rows}


def _timeline():
    return timeline_record([_context_record()])


def test_one_node_per_row_in_line_order_joined_by_edges():
    dot = timeline_dot(_timeline())
    assert dot.count("[label=") == 4
    assert [f"n{i} -> n{i + 1};" in dot for i in range(3)] == [True] * 3
    assert "n3 -> " not in dot
    # Record-id order: the first node is the earliest line (offset -2).
    first = next(line for line in dot.splitlines() if line.strip().startswith("n0 ["))
    assert "-2 lines" in first and "-1.0s" not in first


def test_only_the_anchor_is_highlighted_and_same_request_rows_are_marked():
    dot = timeline_dot(_timeline())
    assert dot.count("ANCHOR") == 1
    anchor = next(line for line in dot.splitlines() if "ANCHOR" in line)
    assert anchor.strip().startswith("n2 [") and "0 lines" in anchor
    marked = [line for line in dot.splitlines() if "#e3f0ff" in line]
    assert len(marked) == 1 and marked[0].strip().startswith("n1 [")


def test_log_text_cannot_break_out_of_a_label():
    record = _timeline()
    record["rows"][0]["service"] = 'a" -> n9 [shape=star]; \\ {x} <b> \n"'
    record["rows"][0]["severity"] = "x\\"
    dot = timeline_dot(record)
    label = next(line for line in dot.splitlines() if line.strip().startswith("n0 ["))
    assert "n9" not in dot.replace(label, "")
    assert 'a\\" -> n9' in label  # the quote is escaped, so it stays inside the string
    assert "\n" not in label
    assert dot.count("[label=") == 4


def test_message_text_is_never_drawn():
    record = _timeline()
    record["rows"][0]["message"] = "SECRET-PAYLOAD"
    assert "SECRET-PAYLOAD" not in timeline_dot(record)


def test_missing_numbers_do_not_crash():
    record = _timeline()
    record["rows"][1]["seconds"] = None
    record["rows"][1]["offset"] = None
    assert "? lines · ?" in timeline_dot(record)


def test_nodes_are_capped():
    record = _timeline()
    record["rows"] = [
        {"offset": i, "record_id": i, "seconds": float(i), "severity": "INFO", "service": "s"}
        for i in range(MAX_NODES + 25)
    ]
    record["arguments"]["anchor_record_id"] = 3
    assert timeline_dot(record).count("[label=") == MAX_NODES


@pytest.mark.parametrize(
    "record",
    [None, "x", {}, {"tool_name": "timeline", "rows": []},
     {"tool_name": "timeline", "rows": [{"record_id": 1}]},
     {"tool_name": "get_trend_tool", "rows": [{"record_id": 1}, {"record_id": 2}]}],
)
def test_nothing_to_draw_returns_none(record):
    assert timeline_dot(record) is None


@pytest.mark.skipif(shutil.which("dot") is None, reason="graphviz binary not installed")
def test_the_dot_is_valid_graphviz():
    record = _timeline()
    record["rows"][0]["service"] = 'q"uote\\ {brace} <angle>'
    result = subprocess.run(["dot", "-Tplain"], input=timeline_dot(record),
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


class _Container:
    def __init__(self):
        self.calls = []

    def markdown(self, text):
        self.calls.append(("markdown", text))

    def dataframe(self, df, **kwargs):
        self.calls.append(("dataframe", df))

    def graphviz_chart(self, dot, **kwargs):
        self.calls.append(("graphviz", dot))


def test_the_renderer_draws_the_table_then_one_chart_for_a_timeline():
    record = _timeline()
    container = _Container()
    render_answer(container, f"Around the failure:\n[[RESULT:{record['id']}]]", [record])
    kinds = [kind for kind, _ in container.calls]
    assert kinds == ["markdown", "dataframe", "graphviz"]
    assert isinstance(container.calls[1][1], pd.DataFrame)
    assert container.calls[2][1] == timeline_dot(record)


def test_a_citation_repeated_does_not_draw_a_second_chart():
    record = _timeline()
    container = _Container()
    render_answer(container, f"[[RESULT:{record['id']}]]\n\n[[RESULT:{record['id']}]]", [record])
    assert [k for k, _ in container.calls].count("graphviz") == 1


def test_other_results_get_no_chart():
    container = _Container()
    render_answer(container, "[[RESULT:R1]]", [{"id": "R1", "rows": [{"a": 1}]}])
    assert [k for k, _ in container.calls] == ["dataframe"]
