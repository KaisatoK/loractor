"""Offline tests for the fictional team-ownership table and its loader."""

from contextlib import contextmanager

import pytest

from backend.db import connection, ownership
from backend.db.config import OWNERSHIP_CSV_PATH


def test_shipped_ownership_csv_is_valid_and_covers_both_datasets():
    rows = ownership.read_ownership_csv(OWNERSHIP_CSV_PATH)
    datasets = {dataset for dataset, _, _ in rows}
    assert {"OpenStack", "BGL"} <= datasets
    # The main nova components have an owner.
    owned = {component for dataset, component, _ in rows if dataset == "OpenStack"}
    assert {
        "nova.osapi_compute.wsgi.server",
        "nova.compute.manager",
        "nova.virt.libvirt.imagecache",
        "nova.scheduler.host_manager",
    } <= owned


@pytest.mark.parametrize(
    "body, message",
    [
        ("dataset,service,team\nOpenStack,a,T\n", "header"),
        ("dataset,component,team\nOpenStack,,T\n", "empty field"),
        ("dataset,component,team\nOpenStack,a,T\nOpenStack,a,U\n", "mapped twice"),
    ],
)
def test_read_ownership_csv_rejects_malformed_input(tmp_path, body, message):
    path = tmp_path / "ownership.csv"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        ownership.read_ownership_csv(path)


def test_load_ownership_recreates_table_and_inserts_rows(monkeypatch, tmp_path):
    path = tmp_path / "ownership.csv"
    path.write_text("dataset,component,team\nOpenStack,a,T\nBGL,KERNEL,K\n", encoding="utf-8")
    executed = []

    class _Cursor:
        def execute(self, statement, params=None):
            executed.append((statement, params))

    @contextmanager
    def fake_cursor():
        yield _Cursor()

    monkeypatch.setattr(connection, "db_cursor", fake_cursor)
    assert ownership.load_ownership(path) == 2
    inserted = [params for _, params in executed if params]
    assert inserted == [("OpenStack", "a", "T"), ("BGL", "KERNEL", "K")]
