"""Tests for the temporary demo auto-login seam."""

from pathlib import Path

import backend.appdb.demo as backend_demo
from ui.login import demo


def test_enabled_defaults_to_true_when_unset(monkeypatch):
    monkeypatch.delenv("AUTH_DEMO_AUTOLOGIN", raising=False)
    assert demo.enabled() is True


def test_enabled_false_when_flag_false(monkeypatch):
    monkeypatch.setenv("AUTH_DEMO_AUTOLOGIN", "false")
    assert demo.enabled() is False


def test_enabled_true_when_flag_true(monkeypatch):
    monkeypatch.setenv("AUTH_DEMO_AUTOLOGIN", "true")
    assert demo.enabled() is True


def test_credentials_default(monkeypatch):
    monkeypatch.delenv("AUTH_DEMO_USER", raising=False)
    monkeypatch.delenv("AUTH_DEMO_PASSWORD", raising=False)
    assert demo.username() == "abc"
    assert demo.password() == "12345678"


def test_credentials_honour_env_overrides(monkeypatch):
    monkeypatch.setenv("AUTH_DEMO_USER", "someone")
    monkeypatch.setenv("AUTH_DEMO_PASSWORD", "hunter2secret")
    assert demo.username() == "someone"
    assert demo.password() == "hunter2secret"


def test_backend_demo_provisions_and_is_idempotent(app_db, monkeypatch, capsys):
    monkeypatch.delenv("AUTH_DEMO_USER", raising=False)
    monkeypatch.delenv("AUTH_DEMO_PASSWORD", raising=False)
    assert backend_demo.main() == 0
    assert "Created user 'abc'" in capsys.readouterr().out
    assert backend_demo.main() == 0
    assert "already taken" in capsys.readouterr().out


def test_logout_shows_sign_in_page_instead_of_re_autologin(monkeypatch):
    """Log out must stick for the rest of the session (hermetic, no backend)."""
    import ui.chat_client as chat_client
    from streamlit.testing.v1 import AppTest

    monkeypatch.delenv("AUTH_DEMO_AUTOLOGIN", raising=False)
    monkeypatch.setattr(
        chat_client,
        "login",
        lambda username, password: {
            "token": "t",
            "user": {"id": "u1", "username": "abc"},
        },
    )
    monkeypatch.setattr(chat_client, "list_chats", lambda token: [])
    monkeypatch.setattr(chat_client, "logout", lambda token: None)

    app = Path(__file__).resolve().parents[1] / "src" / "ui" / "streamlit_app.py"
    at = AppTest.from_file(str(app)).run()
    assert at.session_state["auth"] is not None  # auto-login happened
    assert not at.exception

    at.sidebar.button[0].click().run()  # "Log out" is the first sidebar button
    assert at.session_state["auth"] is None  # logged out for real
    assert at.session_state["demo_logged_out"] is True
    assert not at.exception
    # The sign-in page rendered (main page/new_chat render no title).
    assert [t.value for t in at.title] == ["Loractor Chat Agent"]
