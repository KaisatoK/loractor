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


def _nodes(diagram):
    """The node definitions ``n3["..."]`` in node-number order (not grid order)."""
    found = re.findall(r'n(\d+)\["[^"\n]*"\]', diagram)
    by_index = {int(m.group(1)): m.group(0) for m in re.finditer(r'n(\d+)\["[^"\n]*"\]', diagram)}
    assert len(found) == len(by_index)
    return [by_index[i] for i in sorted(by_index)]


def _classes(diagram):
    return dict(re.findall(r"^  class (n\d+) (\w+)$", diagram, flags=re.M))


def test_one_node_per_row_in_line_order_joined_by_arrows():
    diagram = timeline_mermaid(_timeline())
    assert "block-beta" in diagram  # a real grid, so the arrows can cross rows
    assert len(_nodes(diagram)) == 4
    assert [f"  n{i} --> n{i + 1}" in diagram.splitlines() for i in range(3)] == [True] * 3
    # Record-id order: the first node is the earliest line (offset -2).
    assert _nodes(diagram)[0].startswith('n0["-2 ·')


def test_only_the_anchor_is_highlighted_and_same_request_rows_are_marked():
    diagram = timeline_mermaid(_timeline())
    assert diagram.count("ANCHOR") == 1
    assert "ANCHOR" in _nodes(diagram)[2]
    classes = _classes(diagram)
    assert classes["n2"] == "anchor" and classes["n1"] == "request"
    assert sorted(classes.values()) == ["anchor", "plain", "plain", "request"]


def test_nodes_are_two_short_lines():
    for node in _nodes(timeline_mermaid(_timeline())):
        assert node.count("<br/>") == 1


def test_the_chain_snakes_so_every_turn_between_rows_is_drawn():
    record = _timeline()
    record["rows"] = [
        {"offset": i, "record_id": i, "seconds": float(i), "severity": "INFO", "service": "s"}
        for i in range(10)
    ]
    record["arguments"]["anchor_record_id"] = 3
    lines = timeline_mermaid(record).splitlines()
    grid = [ln for ln in lines if ln.startswith("  n") and "[" in ln]
    order = [[int(n) for n in re.findall(r'n(\d+)\["', ln)] for ln in grid]
    spaces = [ln.count("space") for ln in grid]
    assert order[0] == [0, 1, 2, 3]
    assert order[1] == [7, 6, 5, 4]  # the second row runs right to left
    assert order[2] == [8, 9]
    assert spaces == [0, 0, 0]
    # every consecutive pair is joined, including the turns 3 -> 4 and 7 -> 8
    assert all(f"  n{i} --> n{i + 1}" in lines for i in range(9))


def test_a_short_odd_row_stays_under_the_end_of_the_row_above():
    record = _timeline()
    record["rows"] = [
        {"offset": i, "record_id": i, "seconds": float(i), "severity": "INFO", "service": "s"}
        for i in range(6)
    ]
    record["arguments"]["anchor_record_id"] = 3
    second = [ln for ln in timeline_mermaid(record).splitlines() if 'n5["' in ln][0]
    assert second.split()[:2] == ["space", "space"]
    assert second.index('n4["') > second.index('n5["')  # n4 is last, under n3


def test_log_text_cannot_break_out_of_a_label():
    record = _timeline()
    record["rows"][0]["service"] = 'a" --> n9["x"]; %% {x} <b>\n"'
    record["rows"][0]["severity"] = "x\\"
    diagram = timeline_mermaid(record)
    first = _nodes(diagram)[0]
    # Only our own delimiters survive: the opening and closing quote of the label.
    assert first.count('"') == 2
    assert "<b>" not in diagram and "--> n9" not in diagram
    assert "#34;" in first and "#60;" in first and "#37;" in first
    assert "\n" not in first
    assert len(_nodes(diagram)) == 4


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
    diagram = timeline_mermaid(record)
    assert len(_nodes(diagram)) == MAX_NODES
    assert "ANCHOR" in diagram  # the window is centred on the anchor, not the first rows


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
