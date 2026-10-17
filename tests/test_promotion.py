"""Promotion: budget, serialization, digest, and teardown.

Promotion is the only operation in the sweep that reads message text, and text
costs ~660 s per row group. A chunk is one row group, so promotion is measured
in minutes and everything here follows from that: it is serialized, capped,
priced before it runs, and it keeps a digest rather than the rows.

Driven against a stub cursor. The real operation takes eleven minutes per chunk,
which is not a thing to run in a test suite, and the properties worth protecting
are structural rather than numeric.
"""

from __future__ import annotations

import re
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from sql_render import render  # noqa: E402

from backend.db import promotion  # noqa: E402
from backend.db.budget import ALLOWANCES, PROBLEM_SEVERITIES  # noqa: E402
from backend.db.chunks import Chunk  # noqa: E402
from backend.db.errors import QueryRefused  # noqa: E402

START = datetime(2025, 9, 23, tzinfo=UTC)


@pytest.fixture
def chunk():
    return Chunk(
        chunk_id="part_000:12-12",
        url="https://example.invalid/text_bench/part_000.parquet",
        first_row_group=12,
        last_row_group=12,
        num_rows=614_400,
        compressed_bytes=25_240_000,
        ts_lo=START,
        ts_hi=START + timedelta(seconds=1, microseconds=100_000),
    )


class StubCursor:
    """Responds to promotion's queries with plausible aggregates."""

    def __init__(self, *, db_bytes=40_000_000, leak_bytes=0, rows=90_000):
        self.db_bytes = db_bytes
        self.leak_bytes = leak_bytes
        self.rows = rows
        self.statements: list[str] = []
        self.rowcount = 0
        self._result: list[dict] = []
        self._dropped = False

    def execute(self, query, params=None):
        text = query if isinstance(query, str) else render(query)
        flat = re.sub(r"\s+", " ", text).strip()
        self.statements.append(flat)
        self._result = []

        # Specific shapes before generic ones: every aggregate below also
        # contains count(*), so matching that first would answer them all with
        # a row count.
        if "pg_database_size" in flat:
            extra = self.leak_bytes if self._dropped else 0
            self._result = [{"b": self.db_bytes + extra}]
        elif flat.startswith("DROP TABLE"):
            self._dropped = True
        elif "pg_total_relation_size" in flat:
            self._result = [{"b": 14_000_000}]
        elif '"SeverityText" AS k' in flat:
            self._result = [
                {"k": "error", "n": 80_000},
                {"k": "warn", "n": 9_000},
                {"k": "fatal", "n": 1_000},
            ]
        elif '"ServiceName" AS k' in flat:
            self._result = [
                {"k": "frontend", "n": 70_000},
                {"k": "payment", "n": 20_000},
            ]
        elif '"EventTemplate" AS t' in flat:
            self._result = [
                {"t": "failed to charge card: <*>", "n": 40_000},
                {"t": "failed to place order", "n": 30_000},
            ]
        elif "count(*)" in flat:
            self._result = [{"n": self.rows}]
        elif '"Body" AS message' in flat:
            self._result = [
                {
                    "ts": f"2025-09-23 00:00:0{i}+00",
                    "severity": "fatal" if i == 0 else "error",
                    "service": "frontend",
                    "message": f"failed to charge card: attempt {i}",
                    "trace_id": f"trace{i}",
                }
                for i in range(3)
            ]

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return self._result


class StubContext:
    def __init__(self, cursor):
        self.cursor = cursor

    def __enter__(self):
        return self.cursor

    def __exit__(self, *exc):
        return False


@pytest.fixture
def stub(monkeypatch):
    def install(cursor=None):
        cursor = cursor or StubCursor()
        monkeypatch.setattr(
            promotion, "_promotion_cursor", lambda: StubContext(cursor)
        )
        return cursor

    return install


# --- Naming and cost --------------------------------------------------------
def test_a_chunk_id_becomes_a_usable_table_name():
    """Ids carry a colon and a hyphen, so they are sanitised not quoted."""
    name = promotion.promoted_table("part_000:12-12")
    assert name == "otel_promoted_part_000_12_12"
    assert re.fullmatch(r"[0-9a-zA-Z_]+", name)


def test_promotion_cost_comes_from_the_measured_text_read(chunk):
    """~660 s per row group, measured, not guessed."""
    assert promotion.estimate_promotion_seconds(chunk) == pytest.approx(660, rel=0.01)


def test_four_promotions_are_a_declarable_commitment(chunk):
    """The reason Task 17 asks for confirmation: this is three quarters of an
    hour, and a user should know before it starts, not after."""
    assert promotion.estimate_promotion_seconds(chunk) * 4 > 2_400


def test_capacity_explains_why_only_one_runs_at_a_time():
    capacity = promotion.promotion_capacity()
    assert capacity["concurrent_promotions"] == 1
    assert capacity["allowance_bytes"] == ALLOWANCES["otel_chunk"]
    assert "serialized" in capacity["note"]


# --- Serialization ----------------------------------------------------------
def test_a_second_concurrent_promotion_is_refused(chunk, stub):
    """Overlapping index builds took the server down during development.

    Refusing is the whole safety mechanism, so it is asserted directly rather
    than trusted to a comment.
    """
    stub()
    promotion._PROMOTION_LOCK.acquire()
    try:
        with pytest.raises(QueryRefused) as excinfo:
            promotion.promote_chunk(chunk)
        assert excinfo.value.refusal.code == "promotion_busy"
    finally:
        promotion._PROMOTION_LOCK.release()


def test_the_lock_is_released_even_when_a_promotion_fails(chunk, stub, monkeypatch):
    """A leaked lock would wedge every later promotion in the process."""
    stub()
    monkeypatch.setattr(
        promotion,
        "_materialize",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("build failed")),
    )
    with pytest.raises(RuntimeError):
        promotion.promote_chunk(chunk)

    assert promotion._PROMOTION_LOCK.acquire(blocking=False)
    promotion._PROMOTION_LOCK.release()


def test_the_lock_is_released_after_a_successful_promotion(chunk, stub):
    stub()
    promotion.promote_chunk(chunk)
    assert promotion._PROMOTION_LOCK.acquire(blocking=False)
    promotion._PROMOTION_LOCK.release()


def test_promotions_from_two_threads_do_not_overlap(chunk, stub):
    stub()
    overlaps: list[int] = []
    active = {"n": 0}
    guard = threading.Lock()
    original = promotion._materialize

    def counting_materialize(*args, **kwargs):
        with guard:
            active["n"] += 1
            overlaps.append(active["n"])
        try:
            return original(*args, **kwargs)
        finally:
            with guard:
                active["n"] -= 1

    promotion._materialize = counting_materialize
    try:
        results: list[str] = []

        def run():
            try:
                promotion.promote_chunk(chunk)
                results.append("ok")
            except QueryRefused:
                results.append("refused")

        threads = [threading.Thread(target=run) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert max(overlaps) == 1
        assert len(results) == 4
    finally:
        promotion._materialize = original


# --- Budget -----------------------------------------------------------------
def test_an_over_budget_promotion_is_refused_before_building(chunk, stub):
    from backend.db.budget import BUDGET

    cursor = stub(StubCursor(db_bytes=BUDGET.total_disk_bytes - 1_000_000))
    with pytest.raises(QueryRefused) as excinfo:
        promotion.promote_chunk(chunk)

    assert excinfo.value.refusal.code == "over_budget"
    assert not any(s.startswith("CREATE TABLE") for s in cursor.statements)


def test_the_budget_uses_rows_kept_not_rows_read(chunk, stub):
    """A chunk reads ~614k rows but keeps ~15% of them.

    Budgeting on rows read would refuse chunks that fit comfortably.
    """
    cursor = stub()
    promotion.promote_chunk(chunk)
    assert any(s.startswith("CREATE TABLE") for s in cursor.statements)


# --- The digest -------------------------------------------------------------
def test_the_digest_records_both_rows_read_and_rows_kept(chunk, stub):
    stub()
    digest = promotion.promote_chunk(chunk)
    assert digest.rows_scanned == chunk.num_rows
    assert digest.rows_indexed == 90_000


def test_the_digest_keeps_verbatim_exemplars_with_provenance(chunk, stub):
    """An aggregate cannot be quoted. A digest that says '12,000 failures'
    without a real line is not evidence anyone can act on."""
    stub()
    digest = promotion.promote_chunk(chunk)

    assert digest.exemplars
    for exemplar in digest.exemplars:
        assert exemplar["file"] == "part_000.parquet"
        assert exemplar["chunk_id"] == chunk.chunk_id
        assert exemplar["ts"]
        assert exemplar["message"]


def test_exemplars_lead_with_the_worst_severity(chunk, stub):
    """Reproducible ordering, so a cited digest quotes the same lines twice."""
    cursor = stub()
    promotion.promote_chunk(chunk)
    exemplar_query = next(s for s in cursor.statements if '"Body" AS message' in s)
    assert "fatal" in exemplar_query
    assert "ORDER BY" in exemplar_query


def test_the_digest_ranks_templates_by_volume(chunk, stub):
    stub()
    digest = promotion.promote_chunk(chunk)
    counts = [count for _, count in digest.top_templates]
    assert counts == sorted(counts, reverse=True)


def test_the_digest_counts_problem_rows(chunk, stub):
    stub()
    digest = promotion.promote_chunk(chunk)
    assert digest.problem_rows == 90_000


def test_the_digest_carries_the_score_that_justified_promoting_it(chunk, stub):
    stub()
    digest = promotion.promote_chunk(chunk, triage_score=0.82)
    assert digest.triage_score == 0.82


def test_the_digest_serializes_without_urls(chunk, stub):
    """Matching the chunk map: object-storage URLs are not the agent's
    business and must not leak into a tool result."""
    stub()
    payload = promotion.promote_chunk(chunk).as_dict()
    assert "https://" not in str(payload)
    assert "url" not in payload


def test_the_digest_reports_its_own_time_range(chunk, stub):
    stub()
    payload = promotion.promote_chunk(chunk).as_dict()
    assert payload["time_range"] == [chunk.ts_lo.isoformat(), chunk.ts_hi.isoformat()]


# --- Teardown ---------------------------------------------------------------
def test_a_promoted_chunk_is_dropped_and_vacuumed(chunk, stub):
    cursor = stub()
    promotion.promote_chunk(chunk)
    assert any(s.startswith("DROP TABLE") for s in cursor.statements)
    assert "VACUUM" in cursor.statements


def test_keep_leaves_the_table_in_place(chunk, stub):
    """For an interactive caller that wants to query what it just paid for."""
    cursor = stub()
    promotion.promote_chunk(chunk, keep=True)
    drops = [s for s in cursor.statements if s.startswith("DROP TABLE")]
    # Only the pre-build drop, not a teardown drop.
    assert len(drops) == 1
    assert "VACUUM" not in cursor.statements


def test_a_leak_is_detected_and_recorded(chunk, stub):
    """Measured behaviour is that teardown returns disk exactly.

    A sweep over hundreds of chunks depends on that, so it is verified per
    promotion rather than assumed once.
    """
    stub(StubCursor(leak_bytes=8 * 1024 * 1024))
    digest = promotion.promote_chunk(chunk)
    assert any("bytes on disk" in note for note in digest.notes)


def test_no_leak_leaves_no_note(chunk, stub):
    stub(StubCursor(leak_bytes=0))
    digest = promotion.promote_chunk(chunk)
    assert digest.notes == ()


def test_orphaned_promotions_can_be_swept_up(stub):
    """An interrupted sweep leaves tables behind; names are predictable so they
    can be found without knowing which chunks were in flight."""
    cursor = stub()
    cursor._result = []
    result = promotion.drop_all_promoted()
    assert "freed_bytes" in result
    assert any("information_schema" in s for s in cursor.statements)


# --- SQL shape --------------------------------------------------------------
def test_the_extract_is_bounded_by_the_chunk_and_by_severity(chunk, stub):
    cursor = stub()
    promotion.promote_chunk(chunk)
    create = next(s for s in cursor.statements if s.startswith("CREATE TABLE"))

    assert "Timestamp" in create
    assert "SeverityText" in create
    for severity in PROBLEM_SEVERITIES:
        assert f"'{severity}'" in create


def test_the_extract_uses_an_unordered_window_function(chunk, stub):
    """An ordered window over remote Parquet sorts the whole extract and took
    the server down (budget.py finding 13)."""
    cursor = stub()
    promotion.promote_chunk(chunk)
    create = next(s for s in cursor.statements if s.startswith("CREATE TABLE"))
    assert "row_number() OVER ()" in create
    assert "OVER (ORDER BY" not in create


def test_the_index_is_built_after_materializing(chunk, stub):
    """Indexing a view over remote Parquet re-reads the remote data: measured
    9.77 s against 0.71 s for a local table."""
    cursor = stub()
    promotion.promote_chunk(chunk)
    create_at = next(
        i for i, s in enumerate(cursor.statements) if s.startswith("CREATE TABLE")
    )
    index_at = next(
        i for i, s in enumerate(cursor.statements) if s.startswith("CREATE INDEX")
    )
    assert create_at < index_at


# --- Failure must not leak disk ---------------------------------------------
def test_a_failed_summary_still_returns_the_disk(chunk, stub, monkeypatch):
    """Measured: a promotion that failed while summarising left 43 MB behind.

    A sweep allowed four promotions would consume its whole allowance on four
    failures and then refuse the next one for lack of room, with nothing to
    show for it.
    """
    cursor = stub()
    monkeypatch.setattr(
        promotion,
        "_summarise",
        lambda *a, **k: (_ for _ in ()).throw(ValueError("bad timestamp")),
    )

    with pytest.raises(ValueError):
        promotion.promote_chunk(chunk)

    assert any(s.startswith("DROP TABLE") for s in cursor.statements)
    assert "VACUUM" in cursor.statements


def test_keep_does_not_tear_down_after_a_failure(chunk, stub, monkeypatch):
    """`keep` means the caller wants the table; that holds on failure too,
    since a half-built table is exactly what they would want to inspect."""
    cursor = stub()
    monkeypatch.setattr(
        promotion,
        "_summarise",
        lambda *a, **k: (_ for _ in ()).throw(ValueError("bad timestamp")),
    )

    with pytest.raises(ValueError):
        promotion.promote_chunk(chunk, keep=True)

    assert "VACUUM" not in cursor.statements


def test_the_extract_truncates_the_nanosecond_timestamp(chunk, stub):
    """CREATE TABLE AS inherits the source type.

    The remote corpus is TIMESTAMPTZ_NS, so without truncation the *local*
    table is too, and every later SELECT of it fails in the driver with
    "microsecond must be in 0..999999". Measured: it cost a nine-minute
    promotion that completed its extract and then threw while summarising.
    """
    cursor = stub()
    promotion.promote_chunk(chunk)
    create = next(s for s in cursor.statements if s.startswith("CREATE TABLE"))
    assert "date_trunc('microsecond', \"Timestamp\")" in create
