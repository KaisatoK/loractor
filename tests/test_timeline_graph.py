"""The timeline diagram: Mermaid built in code from the derived ``timeline`` record."""

import re

import pandas as pd
import pytest

from backend.pipeline.timeline import timeline_record
from ui.rendering import render_answer
from ui.timeline_graph import MAX_NODES, timeline_mermaid


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


def _node_lines(diagram):
    return [line for line in diagram.splitlines() if re.match(r"\s+n\d+\[", line)]


def test_one_node_per_row_in_line_order_joined_by_one_chain():
    diagram = timeline_mermaid(_timeline())
    assert diagram.startswith("flowchart LR")  # left to right keeps the chat compact
    assert len(_node_lines(diagram)) == 4
    assert "  n0 --> n1 --> n2 --> n3" in diagram.splitlines()
    # Record-id order: the first node is the earliest line (offset -2).
    assert _node_lines(diagram)[0].strip().startswith('n0["-2 ·')


def test_only_the_anchor_is_highlighted_and_same_request_rows_are_marked():
    diagram = timeline_mermaid(_timeline())
    assert diagram.count("ANCHOR") == 1
    anchor = next(line for line in _node_lines(diagram) if "ANCHOR" in line)
    assert anchor.strip().startswith("n2[") and anchor.endswith(":::anchor")
    marked = [line for line in _node_lines(diagram) if line.endswith(":::request")]
    assert len(marked) == 1 and marked[0].strip().startswith("n1[")
    assert sum(line.endswith(":::plain") for line in _node_lines(diagram)) == 2


def test_nodes_are_two_short_lines():
    for line in _node_lines(timeline_mermaid(_timeline())):
        assert line.count("<br/>") == 1


def test_log_text_cannot_break_out_of_a_label():
    record = _timeline()
    record["rows"][0]["service"] = 'a" --> n9["x"]; %% {x} <b>\n"'
    record["rows"][0]["severity"] = "x\\"
    diagram = timeline_mermaid(record)
    first = _node_lines(diagram)[0]
    # Only our own delimiters survive: the opening and closing quote of the label.
    assert first.count('"') == 2
    assert "<b>" not in diagram and "n9" not in diagram.replace(first, "")
    assert "#34;" in first and "#60;" in first and "#37;" in first
    assert "\n" not in first
    assert len(_node_lines(diagram)) == 4


def test_message_text_is_never_drawn():
    record = _timeline()
    record["rows"][0]["message"] = "SECRET-PAYLOAD"
    assert "SECRET-PAYLOAD" not in timeline_mermaid(record)


def test_missing_numbers_do_not_crash():
    record = _timeline()
    record["rows"][1]["seconds"] = None
    record["rows"][1]["offset"] = None
    assert '["? · ?<br/>' in timeline_mermaid(record)


def test_long_service_names_are_cut():
    record = _timeline()
    record["rows"][0]["service"] = "s" * 200
    assert "s" * 100 not in timeline_mermaid(record)


def test_nodes_are_capped():
    record = _timeline()
    record["rows"] = [
        {"offset": i, "record_id": i, "seconds": float(i), "severity": "INFO", "service": "s"}
        for i in range(MAX_NODES + 25)
    ]
    record["arguments"]["anchor_record_id"] = 3
    assert len(_node_lines(timeline_mermaid(record))) == MAX_NODES


@pytest.mark.parametrize(
    "record",
    [None, "x", {}, {"tool_name": "timeline", "rows": []},
     {"tool_name": "timeline", "rows": [{"record_id": 1}]},
     {"tool_name": "get_trend_tool", "rows": [{"record_id": 1}, {"record_id": 2}]}],
)
def test_nothing_to_draw_returns_none(record):
    assert timeline_mermaid(record) is None


class _Container:
    def __init__(self):
        self.calls = []

    def markdown(self, text):
        self.calls.append(("markdown", text))

    def dataframe(self, df, **kwargs):
        self.calls.append(("dataframe", df))

    def mermaid_chart(self, body, **kwargs):
        self.calls.append(("mermaid", body))


def test_the_renderer_draws_the_table_then_one_chart_for_a_timeline():
    record = _timeline()
    container = _Container()
    render_answer(container, f"Around the failure:\n[[RESULT:{record['id']}]]", [record])
    assert [kind for kind, _ in container.calls] == ["markdown", "dataframe", "mermaid"]
    assert isinstance(container.calls[1][1], pd.DataFrame)
    assert container.calls[2][1] == timeline_mermaid(record)


def test_a_citation_repeated_does_not_draw_a_second_chart():
    record = _timeline()
    container = _Container()
    render_answer(container, f"[[RESULT:{record['id']}]]\n\n[[RESULT:{record['id']}]]", [record])
    assert [k for k, _ in container.calls].count("mermaid") == 1


def test_other_results_get_no_chart():
    container = _Container()
    render_answer(container, "[[RESULT:R1]]", [{"id": "R1", "rows": [{"a": 1}]}])
    assert [k for k, _ in container.calls] == ["dataframe"]


def test_the_same_diagram_twice_in_one_run_is_accepted_by_streamlit(tmp_path):
    from streamlit.testing.v1 import AppTest

    script = tmp_path / "app.py"
    script.write_text(
        "import streamlit as st\n"
        "st.mermaid_chart('flowchart LR\\n a-->b')\n"
        "st.mermaid_chart('flowchart LR\\n a-->b')\n"
    )
    assert not AppTest.from_file(str(script)).run().exception
