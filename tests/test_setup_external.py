"""Offline checks for the setup.sh external-index option and external CLI."""

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import subprocess

import pytest

from backend.db import external

ROOT = Path(__file__).resolve().parents[1]


def test_default_index_window_is_first_5_seconds(monkeypatch):
    source = SimpleNamespace(
        sources=("part_000.parquet",),
        field=lambda name: SimpleNamespace(column="Timestamp"),
    )
    monkeypatch.setattr(external, "resolve", lambda _: source)
    monkeypatch.setattr(
        external,
        "load_row_groups",
        lambda url, column: [
            SimpleNamespace(ts_min=datetime(2025, 9, 23, tzinfo=timezone.utc))
        ],
    )

    assert external.default_index_window() == (
        "2025-09-23 00:00:00.000000+00:00",
        "2025-09-23 00:00:05.000000+00:00",
    )


def test_external_main_forwards_explicit_bounds_to_ensure_searchable(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(
        external,
        "ensure_searchable",
        lambda start, end: calls.append((start, end)) or {"built": True},
    )

    external.main(["START+00", "END+00"])

    assert calls == [("START+00", "END+00")]
    assert '"built": true' in capsys.readouterr().out


def test_setup_external_index_is_exclusive_and_documented():
    result = subprocess.run(
        ["bash", "setup.sh", "--external-index", "openstack"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 2
    assert "cannot be combined with dataset names" in result.stderr
    assert "--external-index" in Path(ROOT / "setup.sh").read_text()


def test_external_main_rejects_one_bound():
    with pytest.raises(SystemExit):
        external.main(["START+00"])
