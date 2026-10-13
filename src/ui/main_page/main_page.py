import json

import pyperclip
import streamlit as st

from ui import chat_client
from ui.rendering import MARKER_RE, render_answer
from ui.roles import ROLE_LABELS


def _persist(*args, **kwargs) -> None:
    """Persist a message, surfacing (but not crashing on) backend errors."""
    try:
        chat_client.append_message(*args, **kwargs)
    except Exception as err:  # noqa: BLE001
        st.warning(f"Message could not be saved: {err}")


@st.fragment(key="copy_to_clipboard_button")
def copy_to_clipboard_button(content: str, role: str, idx: int):
    if st.button(
        label="Copy to Clipboard",
        icon=":material/content_copy:",
        key=f"copy_{role}_{hash(content)}_{idx}",
    ):
        pyperclip.copy(content)
        st.toast(body="Copied to clipboard!", duration=1)


# Backend run statuses (mirrors backend/jobs.py).
RUNNING = "running"
DONE = "done"
ERROR = "error"
CANCELLED = "cancelled"
_TERMINAL = {DONE, ERROR, CANCELLED}


def _new_run(run_id: str, question: str | None) -> dict:
    """Fresh UI-side state for watching one backend run.

    ``log`` is the render buffer (display items in arrival order) and
    ``cycle_start`` is where the currently active cycle begins. The live view
    redraws only ``log[cycle_start:]`` so the thinking of an older revision
    stage does not pile up behind the current one.
    """
    return {
        "run_id": run_id,
        "since": 0,
        "status": RUNNING,
        "log": [{"kind": "progress", "text": "Investigating…"}],
        "cycle_start": 0,
        "progress": [],
        "answer": "",
        "tool_results": [],
        "question": question,
        "result": None,
        "error": None,
    }


def _buffer_progress(run: dict, line: str, *, new_cycle: bool = False) -> None:
    """Record a progress line for persistence and queue it for live rendering.

    ``new_cycle=True`` drops the lines of the previous cycle by moving
    ``cycle_start`` to the new line (used when a new subgraph/stage begins).
    """
    run["progress"].append(line)
    if new_cycle:
        run["cycle_start"] = len(run["log"])
    run["log"].append({"kind": "progress", "text": line})


def _apply_event(run: dict, event: dict) -> None:
    """Fold one streamed event into the run's buffer (renders nothing).

    The live view redraws the buffered lines on its own cadence, so a poll tick
    only ever appends to the buffer instead of driving the screen.
    """
    etype = event.get("type")
    content = event.get("content", "")
    if etype == "messages":
        run["answer"] = content
        if content:
            run["log"].append({"kind": "answer", "text": content})
    elif etype == "tool_results":
        try:
            run["tool_results"] = json.loads(content) or []
        except (TypeError, ValueError):
            run["tool_results"] = []
    elif etype == "tool_progress":
        try:
            op = json.loads(content)
        except (TypeError, ValueError):
            return
        action, status = op.get("operation", ""), op.get("status")
        if status == "completed":
            _buffer_progress(run, f"✓ {action}")
        elif status not in ("started", None):
            _buffer_progress(run, f"{action} — failed")
    elif etype == "subgraph_name":
        # A new subgraph = a new stage/cycle: drop the previous cycle's thinking.
        _buffer_progress(run, f"Currently at stage: {content}", new_cycle=True)
    elif etype == "subgraph_message":
        _buffer_progress(run, f"Thinking: {content}")


def _render_live_item(container, item: dict) -> None:
    """Draw one buffered display item (a thinking line or the streamed answer)."""
    if item["kind"] == "answer":
        container.markdown(MARKER_RE.sub("_loading table…_", item["text"]))
    else:
        container.caption(item["text"])


def _render_live(box, run: dict) -> None:
    """Replace the live box with the current cycle's buffered items.

    ``box`` is an ``st.empty()`` created outside the polling fragment. The
    fragment's writes to an outside container are cleared and redrawn on every
    tick, so drawing *only* new items would wipe them next tick; instead we
    redraw the whole current cycle each time. Writing a fresh child container
    into the empty box replaces the previous tick's render (no accumulation),
    which is what keeps repeated refreshes from stacking up.
    """
    container = box.container()
    with container:
        for item in run["log"][run["cycle_start"] :]:
            _render_live_item(container, item)


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
    st.session_state.active_runs[chat_id] = _new_run(run["run_id"], text)


def _finalize_run(current_chat, chat_id: str, run: dict, live) -> None:
    """Retire a finished run and fold its result back into the chat history."""
    st.session_state.active_runs.pop(chat_id, None)
    status = run["status"]
    if status == DONE:
        result = run.get("result") or {}
        current_chat["history"].append(
            {
                "role": "assistant",
                "content": result.get("answer") or run["answer"],
                "progress": result.get("progress") or run["progress"],
                "tool_results": result.get("tool_results") or run["tool_results"],
            }
        )
        # A single full rerun at the very end (never during streaming) so the
        # finished answer renders through the history loop with its real tables
        # and reframe buttons instead of the plain live placeholder.
        st.rerun(scope="app")
    elif status == ERROR:
        # Drawn into the live container so it survives the fragment's next tick.
        live.error(f"Investigation failed: {run.get('error') or 'unknown error'}")
    # cancelled: the chat was deleted or the run was stopped; render nothing.


@st.fragment(run_every="1s")
def render_active_run(current_chat, token: str, chat_id: str, live) -> None:
    """Poll the chat's in-flight investigation and redraw its live progress.

    Only this fragment refreshes on a timer. Each tick it buffers any new events
    into session state and then redraws the current cycle into ``live`` (an
    ``st.empty()`` created outside the fragment), whose previous content is
    replaced rather than stacked. State is keyed by chat id, so switching
    conversations and returning re-attaches to the same run instead of
    cancelling it.
    """
    run = st.session_state.active_runs.get(chat_id)
    if not run:
        return

    if run["status"] not in _TERMINAL:
        try:
            payload = chat_client.get_run(
                token=token,
                chat_id=chat_id,
                run_id=run["run_id"],
                since=run["since"],
            )
        except Exception as err:  # noqa: BLE001 - the run itself keeps going
            status_code = getattr(getattr(err, "response", None), "status_code", None)
            if status_code == 404:
                # The run is gone (evicted or discarded); stop tracking it and
                # refresh history in case it was persisted before eviction.
                st.session_state.active_runs.pop(chat_id, None)
                try:
                    current_chat["history"] = chat_client.get_messages(token, chat_id)
                except Exception:  # noqa: BLE001 - best effort
                    pass
                st.rerun(scope="app")
                return
            st.warning(f"Lost connection to the investigation: {err}")
            return

        # Buffer only — nothing is drawn here; the redraw happens below.
        for event in payload["events"]:
            _apply_event(run, event)
        run["since"] = payload["next_since"]
        run["status"] = payload["status"]
        run["error"] = payload.get("error")
        if payload.get("result") is not None:
            run["result"] = payload["result"]

    # Redraw the current cycle into the live box so every buffered line stays
    # visible (the fragment's outside-container region is cleared each tick).
    _render_live(live, run)

    if run["status"] in _TERMINAL:
        _finalize_run(current_chat, chat_id, run, live)


def main_page():
    st.title("LangChain Chat Agent")

    token: str = st.session_state.auth["token"]
    chat_id: str = st.session_state.current_chat
    current_chat = st.session_state.chat_histories.get(chat_id, {})

    # Fetch this chat's history from the backend the first time it is opened.
    if current_chat.get("history") is None:
        try:
            current_chat["history"] = chat_client.get_messages(token, chat_id)
        except Exception as err:  # noqa: BLE001
            st.error(f"Could not load this chat: {err}")
            current_chat["history"] = []

        # On a fresh open, re-attach to an investigation started before this
        # session (e.g. after a browser refresh) so its progress is not lost.
        if chat_id not in st.session_state.active_runs:
            try:
                run_id = chat_client.active_run(token=token, chat_id=chat_id)
            except Exception:  # noqa: BLE001 - re-attachment is best-effort
                run_id = None
            if run_id:
                st.session_state.active_runs[chat_id] = _new_run(run_id, None)

    for idx, item in enumerate(current_chat["history"]):
        if (item["role"] == "assistant" and "progress" in item) and item[
            "progress"
        ] is not None:
            with st.status(label="Done", type="compact", state="complete"):
                for stage in item["progress"]:
                    st.caption(stage)
        with st.chat_message(item["role"]):
            if item["role"] == "assistant":
                if item.get("reframed_for"):
                    st.caption(
                        f"Reframed for {ROLE_LABELS[item['reframed_for']]} — "
                        "same evidence, no new collection."
                    )
                render_answer(st, item["content"], item.get("tool_results"))
                if item.get("tool_results"):
                    # Never offer the role the answer is already written for:
                    # originals use the chat's role, reframed items their target.
                    own_role = item.get("reframed_for") or current_chat["role"]
                    targets = [t for t in ROLE_LABELS if t != own_role]
                    # Left-aligned, tightly packed buttons (not full-width columns).
                    with st.container(horizontal=True, horizontal_alignment="left"):
                        for target in targets:
                            if st.button(
                                ROLE_LABELS[target],
                                key=f"reframe_{target}_{idx}",
                                width="content",
                            ):
                                question = next(
                                    (
                                        h["content"]
                                        for h in reversed(current_chat["history"][:idx])
                                        if h["role"] == "user"
                                    ),
                                    "",
                                )
                                try:
                                    with st.spinner(
                                        f"Reframing for {ROLE_LABELS[target]}…"
                                    ):
                                        answer = chat_client.reframe(
                                            token=token,
                                            target_role=target,
                                            effort=st.session_state.get(
                                                "effort", "Medium"
                                            ),
                                            question=question,
                                            previous_answer=item["content"],
                                            tool_results=item.get("tool_results") or [],
                                        )
                                except Exception as e:
                                    st.error(f"Reframe failed: {e}")
                                else:
                                    _persist(
                                        token,
                                        chat_id,
                                        role="assistant",
                                        content=answer,
                                        tool_results=item.get("tool_results") or [],
                                        reframed_for=target,
                                    )
                                    current_chat["history"].append(
                                        {
                                            "role": "assistant",
                                            "content": answer,
                                            "progress": None,
                                            "tool_results": item.get("tool_results")
                                            or [],
                                            "reframed_for": target,
                                        }
                                    )
                                    st.rerun()
            else:
                st.markdown(item["content"])
        copy_to_clipboard_button(item["content"], item["role"], idx)

    # Live view of an in-flight investigation for this chat (if any). The box is
    # an st.empty() created outside the polling fragment; the fragment redraws
    # the current cycle into it each tick, replacing (not stacking) content.
    run = st.session_state.active_runs.get(chat_id)
    if run:
        live = st.empty()
        render_active_run(current_chat, token, chat_id, live)

    col1, col2 = st.bottom.columns(spec=[0.1, 0.9], vertical_alignment="bottom")

    effort = col1.selectbox(
        "Effort Level",
        ["Low", "Medium", "High", "Extreme", "Brutal"],
        index=1,
        help="Low: quick check — fewer tool calls, 1 hypothesis. Brutal: deepest investigation.",
        key="effort",
    )

    message = col2.chat_input(
        placeholder="Type your message here...", accept_file="multiple"
    )

    if message:
        if chat_id in st.session_state.active_runs:
            st.warning("An investigation is already running for this chat.")
        else:
            try:
                start_message(current_chat, message.text, effort, token, chat_id)
            except Exception as err:  # noqa: BLE001 - surface, don't crash
                st.error(f"Could not start the investigation: {err}")
            else:
                # Re-render so the live run (and, when done, its reframe buttons
                # via the history loop) is picked up immediately.
                st.rerun()
