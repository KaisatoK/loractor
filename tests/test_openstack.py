"""OpenStack registration and loader: the second loghub corpus.

Offline, mirroring ``tests/test_bgl.py``. Two claims are under test:

* **adding OpenStack required no new SQL in any query builder** -- the same
  registry-driven builders that serve BGL and OTel compose against the
  ``openstack`` table's own columns;
* **the loader turns three raw nova logs into one table**: a positional parser
  (unlike BGL's vocabulary-anchored one), a ``LineId`` that runs on across the
  files, a derived ``EventTemplate``, and a UTC ``Timestamp``.

OpenStack disagrees with BGL in the ways that matter here: its severities
include ``CRITICAL`` and not ``FATAL``, its entity id is an integer ``Pid``
rather than a node string, and it spans three days rather than seven months.

The raw ``.log`` files are gitignored, so the parser is exercised on real lines
copied from them; streaming is exercised on temporary files with a fake cursor
that records what COPY would have received.
"""

from __future__ import annotations

import csv
import io
import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from sql_render import normalized, render  # noqa: E402

import backend.db as db  # noqa: E402
from backend.db import loghub  # noqa: E402
from backend.db.datasets import (  # noqa: E402
    DEFAULT_DATASET_ID,
    catalog,
    dataset_ids,
    resolve,
)
from backend.db.errors import QueryRefused  # noqa: E402

WINDOW = ("2017-05-14 00:00:00+00", "2017-05-18 00:00:00+00")


def build(fn, *args, **kwargs):
    return fn.__wrapped__(*args, **kwargs)


# --- Registration -----------------------------------------------------------
def test_openstack_is_registered_and_is_the_default():
    assert "openstack" in dataset_ids()
    assert DEFAULT_DATASET_ID == "openstack"


def test_openstack_derives_a_request_id_from_addr():
    """``request_id`` is the first word of ``ADDR`` when it is a ``req-`` id,
    derived at query time, so it needs no new column and no reload."""
    openstack = resolve("openstack")
    request = openstack.field("request_id")
    assert request.column is None and request.expression
    assert '"ADDR"' in request.expression and "'req-'" in request.expression
    # A bare % would be read as a placeholder by psycopg2 in any query with
    # parameters, and every nearby-events query has one.
    assert "%" not in request.expression
    assert "request_id" in catalog()["openstack"]["fields"]


def test_openstack_spec_stores_the_column_request_id_reads():
    spec = loghub.spec_for("openstack")
    assert "ADDR" in set(spec.all_column_names)


# --- The vocabulary each corpus owns ----------------------------------------
def test_a_lowercase_severity_is_refused_for_openstack():
    with pytest.raises(QueryRefused) as excinfo:
        build(db.count_by_field, *WINDOW, dataset="openstack", severity="error")
    refusal = excinfo.value.refusal
    assert refusal.code == "severity_case"
    assert "openstack" in refusal.reason


def test_critical_is_an_accepted_severity():
    query, params, _ = build(
        db.count_by_field, *WINDOW, dataset="openstack", severity="CRITICAL"
    )
    assert '"Level" = %s' in normalized(query)
    assert "CRITICAL" in params


# --- Zero new SQL: the builders are unchanged -------------------------------
def test_the_integer_pid_filters_as_the_entity_id():
    query, params, _ = build(
        db.get_trend, *WINDOW, "entity_id", 2931, dataset="openstack", bucket="hour"
    )
    assert '"Pid" = %s' in normalized(query)
    assert 2931 in params


# --- Loader -----------------------------------------------------------------
API_LINE = (
    "nova-api.log.1.2017-05-16_13:53:08 2017-05-16 00:00:00.008 25746 INFO "
    "nova.osapi_compute.wsgi.server [req-38101a0b-2096-447d-96ea-a692162415ae "
    "113d3a99c3da401fbd62cc2caa5b96d2 54fadb412c4e40cdbaed9335e4c35a9e - - -] "
    '10.11.10.1 "GET /v2/54fadb412c4e40cdbaed9335e4c35a9e/servers/detail '
    'HTTP/1.1" status: 200 len: 1893 time: 0.2477829'
)
INSTANCE_LINE = (
    "nova-compute.log.1.2017-05-16_13:55:31 2017-05-16 00:00:10.302 2931 INFO "
    "nova.virt.libvirt.driver [-] [instance: b9000564-fe1a-409b-b8cc-1e88b294cd1d] "
    "Instance spawned successfully."
)
TRACEBACK_LINE = (
    "nova-compute.log.1.2017-05-17_12:02:35 2017-05-16 16:30:35.360 2931 ERROR "
    "oslo_service.periodic_task Traceback (most recent call last):"
)


# --- Parser ------------------------------------------------------------------
def test_parses_a_line_with_a_request_context():
    row = loghub.parse_openstack_line(API_LINE)
    assert row["Logrecord"] == "nova-api.log.1.2017-05-16_13:53:08"
    assert row["Date"] == "2017-05-16"
    assert row["Time"] == "00:00:00.008"
    assert row["Pid"] == "25746"
    assert row["Level"] == "INFO"
    assert row["Component"] == "nova.osapi_compute.wsgi.server"
    assert row["ADDR"].startswith("req-38101a0b-2096-447d-96ea-a692162415ae ")
    assert row["ADDR"].endswith(" - - -")
    assert row["Content"].startswith('10.11.10.1 "GET /v2/')


def test_the_message_keeps_its_own_brackets_after_the_header():
    row = loghub.parse_openstack_line(INSTANCE_LINE)
    assert row["ADDR"] == "-"
    assert row["Content"] == (
        "[instance: b9000564-fe1a-409b-b8cc-1e88b294cd1d] Instance spawned successfully."
    )


def test_a_line_without_a_bracket_has_an_empty_addr():
    row = loghub.parse_openstack_line(TRACEBACK_LINE)
    assert row["ADDR"] == ""
    assert row["Level"] == "ERROR"
    assert row["Content"] == "Traceback (most recent call last):"


def test_message_spacing_is_preserved():
    row = loghub.parse_openstack_line(API_LINE.replace("status: 200", "status:   200"))
    assert "status:   200" in row["Content"]


def test_known_limitation_a_headerless_message_starting_with_a_bracket():
    """With no header bracket, a message's own leading ``[...]`` is read as
    ADDR. Real nova lines always carry the header bracket, so this documents the
    behaviour rather than endorsing it."""
    line = TRACEBACK_LINE.replace(
        "Traceback (most recent call last):", "[instance: abc] Terminating"
    )
    row = loghub.parse_openstack_line(line)
    assert row["ADDR"] == "instance: abc"
    assert row["Content"] == "Terminating"


@pytest.mark.parametrize(
    "line",
    ["", "garbage", "nova-api.log 2017-05-16 not-a-time 1 INFO x", "a b c d e f g"],
)
def test_an_unparseable_line_returns_none(line):
    assert loghub.parse_openstack_line(line) is None


def test_the_timestamp_is_utc():
    row = loghub.parse_openstack_line(API_LINE)
    assert row["Timestamp"] == "2017-05-16 00:00:00.008+00"


# --- Specs -------------------------------------------------------------------
def test_both_openstack_specs_are_registered():
    assert loghub.spec_for("openstack") is loghub.OPENSTACK_FULL_SPEC
    assert loghub.spec_for("openstack_2k") is loghub.OPENSTACK_SPEC
    assert {"bgl", "bgl_2k", "openstack", "openstack_2k"} <= set(loghub.SPECS)


def test_the_full_spec_loads_three_files_in_chronological_order():
    """abnormal (05-14), normal1 (05-16), normal2 (05-16..05-17): LineId order is
    then time order across the whole table."""
    spec = loghub.OPENSTACK_FULL_SPEC
    assert spec.source_kind is loghub.SourceKind.RAW_LINES
    assert spec.line_parser is loghub.parse_openstack_line
    assert spec.raw_names == (
        "openstack_abnormal.log",
        "openstack_normal1.log",
        "openstack_normal2.log",
    )
    assert [p.name for p in spec.source_paths] == list(spec.raw_names)
    assert all(p.parent == loghub.DATA_DIR for p in spec.source_paths)
    assert spec.expected_rows == 207_820


def test_the_full_spec_schema():
    assert loghub.OPENSTACK_FULL_SPEC.columns == (
        ("LineId", "BIGINT"),
        ("Logrecord", "VARCHAR"),
        ("Date", "DATE"),
        ("Time", "TIME"),
        ("Pid", "BIGINT"),
        ("Level", "VARCHAR"),
        ("Component", "VARCHAR"),
        ("ADDR", "VARCHAR"),
        ("Content", "VARCHAR"),
        ("EventTemplate", "VARCHAR"),
        ("Timestamp", "TIMESTAMPTZ"),
    )


def test_the_sample_spec_derives_a_utc_timestamp_with_quoted_identifiers():
    spec = loghub.OPENSTACK_SPEC
    assert spec.source_kind is loghub.SourceKind.STRUCTURED_CSV
    assert spec.csv_name == "OpenStack_2k.log_structured.csv"
    ((name, expression),) = spec.derived
    assert name == "Timestamp"
    assert "'UTC'" in expression
    assert '"Date"' in expression and '"Time"' in expression


def test_the_sample_csv_headers_match_the_physical_columns():
    """No renames: every physical column is a header of the shipped sample."""
    with loghub.OPENSTACK_SPEC.csv_path.open(newline="", encoding="utf-8") as handle:
        header = next(csv.reader(handle))
    for name, _ in loghub.OPENSTACK_SPEC.columns:
        assert name in header, name


@pytest.mark.parametrize("spec_id", ["openstack", "openstack_2k"])
def test_spec_and_registry_agree(spec_id):
    """Every column the registry maps, indexes or suggests from must exist."""
    spec = loghub.spec_for(spec_id)
    dataset = resolve("openstack")
    physical = set(spec.all_column_names)

    for name, f in dataset.fields.items():
        if f.column:
            assert f.column in physical, (name, f.column)
        if f.text_indexed:
            assert f.column in spec.verbatim_columns + spec.text_columns, name

    token_columns = {col for col, _ in spec.token_columns}
    assert token_columns <= physical
    assert set(spec.verbatim_columns) <= physical
    for char, field_name in dataset.selectors.items():
        if field_name == "message":
            continue  # matched through the verbatim index, not the token one
        column = dataset.fields[field_name].column
        assert column in token_columns, f"selector {char} -> {column}"


# --- Multi-file streaming ----------------------------------------------------
class _FakeCursor:
    """Records COPY bodies. ``as_string`` needs a real connection, so the COPY
    statement is rendered by the test helper instead."""

    def __init__(self):
        self.statements: list[str] = []
        self.rows: list[list[str]] = []

    def copy_expert(self, statement, buffer):
        self.statements.append(statement)
        self.rows.extend(csv.reader(io.StringIO(buffer.getvalue())))


@pytest.fixture
def fake_copy(monkeypatch):
    from psycopg2 import sql

    monkeypatch.setattr(sql.Composed, "as_string", lambda self, _ctx: render(self))
    return _FakeCursor()


def test_line_ids_run_on_across_files(tmp_path, monkeypatch, fake_copy):
    (tmp_path / "a.log").write_text(
        f"{API_LINE}\ngarbage\n{INSTANCE_LINE}\n", encoding="utf-8"
    )
    (tmp_path / "b.log").write_text(f"\n{TRACEBACK_LINE}\n", encoding="utf-8")
    monkeypatch.setattr(loghub, "DATA_DIR", tmp_path)
    spec = replace(loghub.OPENSTACK_FULL_SPEC, raw_names=("a.log", "b.log"))

    copied = loghub._stream_raw_lines(spec, fake_copy, "openstack")

    assert copied == 3
    physical = [name for name, _ in spec.columns]
    rows = [dict(zip(physical, row)) for row in fake_copy.rows]
    assert [row["LineId"] for row in rows] == ["1", "2", "3"]
    assert [row["Level"] for row in rows] == ["INFO", "INFO", "ERROR"]
    # The template is derived from the message; numbers collapse to <*>.
    assert "<*>" in rows[0]["EventTemplate"]
    assert rows[2]["Timestamp"] == "2017-05-16 16:30:35.360+00"
    # One COPY into the shared table, naming every physical column.
    assert '"openstack"' in fake_copy.statements[0]
    assert '"LineId"' in fake_copy.statements[0]


def test_skipped_lines_are_counted_and_reported(tmp_path, monkeypatch, fake_copy, caplog):
    (tmp_path / "a.log").write_text("garbage\nmore garbage\n", encoding="utf-8")
    monkeypatch.setattr(loghub, "DATA_DIR", tmp_path)
    spec = replace(loghub.OPENSTACK_FULL_SPEC, raw_names=("a.log",))

    with caplog.at_level("WARNING"):
        assert loghub._stream_raw_lines(spec, fake_copy, "openstack") == 0
    assert "Skipped 2 unparseable lines" in caplog.text


def test_a_spec_without_a_parser_cannot_stream(fake_copy):
    spec = replace(loghub.OPENSTACK_FULL_SPEC, line_parser=None)
    with pytest.raises(ValueError):
        loghub._stream_raw_lines(spec, fake_copy, "openstack")


def test_a_multi_file_corpus_ignores_a_single_path_override(monkeypatch):
    """One environment variable cannot name three files."""
    spec = replace(loghub.OPENSTACK_FULL_SPEC, path_env="OPENSTACK_LOG_PATH")
    monkeypatch.setenv("OPENSTACK_LOG_PATH", "/elsewhere/x.log")
    assert len(spec.source_paths) == 3


def test_bgl_still_honours_its_path_override(monkeypatch):
    monkeypatch.setenv("BGL_LOG_PATH", "/elsewhere/BGL.log")
    assert loghub.BGL_FULL_SPEC.source_paths == (Path("/elsewhere/BGL.log"),)
