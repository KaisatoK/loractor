"""TEMPORARY demo auto-login.

Everything here exists only so the hackathon demo skips the sign-in step. To remove
the demo:

1. Delete ``src/ui/login/demo.py`` and ``src/backend/appdb/demo.py``.
2. Delete ``tests/test_demo.py``.
3. Delete the demo provisioning comment and the
   ``uv run python -m backend.appdb.demo`` line from ``backend.sh``.
4. In ``src/ui/streamlit_app.py``: delete the ``from ui.login import demo``
   import, delete the demo comment lines above the gate, delete the
   ``demo.mark_logged_out()`` line from ``_logout()``, and reduce the gate to
   ``login_page(); st.stop()``.
5. Delete the "Demo auto-login (temporary)" section from
   ``src/ui/login/README.md`` (the file itself stays — it also documents the
   permanent auth flow).
6. Delete the "Demo auto-login" section from ``README.md``.
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
    raises and restarts the script with the session authenticated.
    """
    sign_in(lambda: chat_client.login(username(), password()))
