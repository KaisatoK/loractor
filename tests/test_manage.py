"""Tests for the user-provisioning CLI."""

import sys

import backend.appdb.manage as manage
from backend.appdb.manage import provision


def test_main_creates_user(app_db, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["manage", "newuser", "password123"])
    assert manage.main() == 0
    assert "Created user 'newuser'" in capsys.readouterr().out


def test_main_requires_two_args(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["manage", "onlyuser"])
    assert manage.main() == 2
    assert "Usage" in capsys.readouterr().out


def test_main_rejects_short_password(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["manage", "user", "short"])
    assert manage.main() == 2


def test_main_rejects_short_username(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["manage", "ab", "password123"])
    assert manage.main() == 2


def test_main_rejects_over_long_username(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["manage", "u" * 65, "password123"])
    assert manage.main() == 2


def test_provision_rejects_over_long_username():
    code, _ = provision("u" * 65, "password123")
    assert code == 2


def test_main_rejects_over_long_password(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["manage", "user", "p" * 257])
    assert manage.main() == 2


def test_main_reports_duplicate(app_db, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["manage", "dup", "password123"])
    assert manage.main() == 0
    assert manage.main() == 1
    assert "already taken" in capsys.readouterr().out
