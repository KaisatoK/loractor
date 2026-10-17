"""Offline tests for the fictional team-ownership table and its loader."""

from contextlib import contextmanager

import pytest

from backend.db import connection, ownership
from backend.db.config import OWNERSHIP_CSV_PATH
from backend.db.datasets import all_datasets
from backend.pipeline import playbooks


def test_shipped_ownership_csv_is_valid_and_covers_all_measured_components():
    rows = ownership.read_ownership_csv(OWNERSHIP_CSV_PATH)
    datasets = {dataset for dataset, _, _ in rows}
    registered_corpora = {dataset.corpus_name for dataset in all_datasets()}
    assert datasets <= registered_corpora
    assert {"OpenStack", "BGL", "OTel"} <= datasets

    by_dataset = {
        dataset: {component for row_dataset, component, _ in rows if row_dataset == dataset}
        for dataset in datasets
    }
    assert {
        "nova.osapi_compute.wsgi.server",
        "nova.compute.manager",
        "nova.virt.libvirt.imagecache",
        "nova.scheduler.host_manager",
        "oslo_service.periodic_task",
        "keystonemiddleware.auth_token",
    } <= by_dataset["OpenStack"]
    assert {
        "KERNEL", "APP", "DISCOVERY", "MMCS", "HARDWARE", "MONITOR",
        "LINKCARD", "CMCS", "BGLMASTER", "SERV_NET",
    } <= by_dataset["BGL"]
    assert {
        "cart", "frontend", "email", "recommendation", "payment",
        "shipping", "checkout", "quote", "currency", "fraud-detection",
        "kafka", "accounting", "ad", "product-catalog", "valkey-cart",
        "load-generator",
    } <= by_dataset["OTel"]


def test_ownership_context_is_grouped_by_corpus():
    context = playbooks.ownership_context()
    assert "OpenStack:" in context and "BGL:" in context and "OTel:" in context
    assert "- unowned: any OpenStack component not listed above" in context
    assert "- Checkout & Payments:" in context
    assert all(name in context for name in ("cart", "checkout", "currency"))


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
