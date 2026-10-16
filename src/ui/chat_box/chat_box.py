"""Type-ahead chat box, a no-build Streamlit custom component.
The frontend reports the draft and the caret's *current word*; this looks it up
via ``/auto-complete`` (required selector ``@`` component, ``^`` event template,
``$`` content; a ``!`` right after the selector means case-sensitive ranking).
Suggestions are merged across the local corpora, so a ``$`` pick inserts a
``Line ID: n (dataset)`` reference.
"""

from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

from ui import chat_client

_DIR = Path(__file__).parent
_component = components.declare_component("chat_box", path=str(_DIR))

_KEY = "chat_box"
_SUBMIT_SEQ_KEY = "chat_box_submit_seq"
_SELECTORS = "@^$"


def _suggestions(token: str, word: str) -> list[dict]:
    """Best-effort suggestions -- a lookup failure must never break the chat.
    Opt-in via a selector: the word must start with ``@``/``^``/``$`` to scope to
    component/template/content; any other word (including ``!word``) suggests
    nothing."""
    word = word.strip()

    if not word or word[0] not in _SELECTORS:
        return []
    try:
        return chat_client.auto_complete(token, word)
    except Exception:  # noqa: BLE001
        return []


def _take_submission() -> str | None:
    """Consume and return a submission the component has posted, else ``None``.
    Reading it before the component re-renders lets a refusal draw *above* the
    box; the sequence guard takes each Enter once (like ``st.chat_input``)."""
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
    ``suggest=False`` skips the lookup during a run. The submit counter is seeded
    from ``submit_seq_base`` so a remounted box's first submits are not dropped."""
    state = st.session_state.get(_KEY) or {}

    if not isinstance(state, dict):
        state = {}

    text = state.get("text", "")
    word = state.get("word", "")

    _component(
        text=text,
        suggestions=_suggestions(token, word) if suggest else [],
        placeholder=placeholder,
        submit_seq_base=st.session_state.get(_SUBMIT_SEQ_KEY, 0),
        key=_KEY,
        default={"text": "", "submitted": "", "word": "", "submitSeq": 0},
    )


def start_message(current_chat, text, effort, token: str, chat_id: str) -> None:
    """Hand the user turn off to the backend as a background investigation.
    The backend owns the run and persists both messages; the UI only watches.
    Local history updates only once accepted, so a failed start changes nothing."""
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
    Keystrokes rerun only this fragment and no lookup runs during an investigation.
    A submit during a run is refused, with the warning above the box."""
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
