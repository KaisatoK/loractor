"""TEMPORARY demo auto-login.
Exists only so the hackathon demo skips sign-in. Removal steps are documented in
``README.md`` / ``src/ui/login/README.md`` (delete this file + ``appdb/demo.py``).
"""

import os

import streamlit as st

from ui import chat_client
from ui.login.login import sign_in

#: Set AUTH_DEMO_AUTOLOGIN=false to fall back to the real sign-in page.
_FLAG = "AUTH_DEMO_AUTOLOGIN"

#: Session key recording an explicit log out, so the gate stops auto-signing in.
_MARKER = "demo_logged_out"


def enabled() -> bool:
    """Whether the demo should sign in automatically."""
    raw = os.getenv(_FLAG)
    if raw is None:
        return True
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def logged_out() -> bool:
    """Whether the user explicitly logged out in this browser session."""
    return bool(st.session_state.get(_MARKER, False))


def mark_logged_out() -> None:
    """Record an explicit log out so the gate stops auto-signing back in."""
    st.session_state[_MARKER] = True


def username() -> str:
    return os.getenv("AUTH_DEMO_USER", "abc")


def password() -> str:
    return os.getenv("AUTH_DEMO_PASSWORD", "12345678")


def auto_login() -> None:
    """Sign in the demo account.
    Returns only on failure: a successful sign-in calls ``st.rerun()``, which
    raises and restarts the script authenticated."""
    sign_in(lambda: chat_client.login(username(), password()))
