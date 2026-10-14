"""Type-ahead chat box, a no-build Streamlit custom component.

The frontend (``index.html`` + ``chat_box.css`` + ``chat_box.js``, no npm build)
reports the draft and the *current word* -- the run of non-space characters the
caret sits in -- as they are typed; this module looks that word up through the
backend's ``/auto-complete`` endpoint and hands the suggestions back for the
dropdown, which opens only while the box is focused. Choosing a suggestion
replaces the words it spans -- a suggestion for a multi-word field value such as
``Removable base files: <*>`` replaces every word of the draft it lines up with,
so the shared words are not duplicated.

A leading selector in the current word scopes the lookup to one indexed column:
``@`` for ``service``, ``#`` for ``event_id`` and ``$`` for ``event_template``;
without one the word is searched across all three.

:func:`chat_box` is the whole widget: as a Streamlit fragment it renders the
component and, on submit, starts the backend investigation via
:func:`start_message`.
"""

from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

from ui import chat_client

_DIR = Path(__file__).parent
_component = components.declare_component("chat_box", path=str(_DIR))

_KEY = "chat_box"
_SUBMIT_SEQ_KEY = "chat_box_submit_seq"


def _suggestions(token: str, word: str) -> list[dict]:
    """Best-effort suggestions -- a lookup failure must never break the chat.

    The word is sent as-is: a leading selector (``@``, ``#`` or ``$``) scopes the
    lookup to ``service``, ``event_id`` or ``event_template`` on the backend.
    """
    if not word.strip():
        return []
    try:
        return chat_client.auto_complete(token, word)
    except Exception:  # noqa: BLE001
        return []


def _take_submission() -> str | None:
    """Consume and return a submission the component has posted, else ``None``.

    Reading it before the component re-renders lets a refusal be drawn *above*
    the box instead of below it. The sequence guard means each Enter is taken
    exactly once, mirroring ``st.chat_input``.
    """
    state = st.session_state.get(_KEY) or {}
    submit_seq = state.get("submitSeq", 0) if isinstance(state, dict) else 0
    if submit_seq > st.session_state.get(_SUBMIT_SEQ_KEY, 0):
        st.session_state[_SUBMIT_SEQ_KEY] = submit_seq
        return state.get("submitted", "") or None
    return None


def _render_component(
    *,
    token: str,
    suggest: bool = True,
    placeholder: str = "Type your message here...",
) -> None:
    """Render the type-ahead component (the draft plus its suggestions).

    Suggestions match the *current word* of the draft -- the run of non-space
    characters the caret sits in -- which the frontend reports alongside the
    text. ``suggest=False`` skips the auto-complete lookup -- used while an
    investigation is already running.
    """
    state = st.session_state.get(_KEY) or {}
    if not isinstance(state, dict):
        state = {}
    text = state.get("text", "")
    word = state.get("word", "")

    _component(
        text=text,
        suggestions=_suggestions(token, word) if suggest else [],
        placeholder=placeholder,
        key=_KEY,
        default={"text": "", "submitted": "", "word": "", "submitSeq": 0},
    )


def start_message(current_chat, text, effort, token: str, chat_id: str) -> None:
    """Hand the user turn off to the backend as a background investigation.

    The backend owns the investigation from here on and persists both messages;
    the UI only watches the run, so switching conversations or deleting the chat
    no longer truncates the answer or erases the thinking. Local history is only
    updated once the backend has accepted the run, so a failed start leaves the
    chat unchanged.
    """
    request_history = [
        {"role": item["role"], "content": item["content"]}
        for item in current_chat["history"]
    ] + [{"role": "user", "content": text}]
    run = chat_client.start_investigation(
        token=token,
        chat_id=chat_id,
        role=current_chat["role"],
        effort=effort,
        question=text,
        history=request_history,
    )
    current_chat["history"].append({"role": "user", "content": text, "progress": None})
    st.session_state.active_runs[chat_id] = {
        "run_id": run["run_id"],
        "since": 0,
        "answer": "",
        "progress": [],
        "tool_results": [],
        "question": text,
    }


@st.fragment
def chat_box(token: str, current_chat: dict, chat_id: str, effort: str) -> None:
    """Type-ahead chat box, mounted as a fragment.

    Keystrokes rerun only this fragment and no auto-complete lookup is made while
    an investigation is already running. A submit during a run is refused, with
    the warning drawn above the box.
    """
    running = chat_id in st.session_state.active_runs
    submitted = _take_submission()
    if running and submitted is not None:
        st.warning("An investigation is already running for this chat.")
    _render_component(token=token, suggest=not running)
    if running or submitted is None:
        return
    try:
        start_message(current_chat, submitted, effort, token, chat_id)
    except Exception as err:  # noqa: BLE001 - surface, don't crash
        st.error(f"Could not start the investigation: {err}")
    else:
        # Re-render so the live run (and, when done, its reframe buttons via the
        # history loop) is picked up immediately.
        st.rerun(scope="app")
