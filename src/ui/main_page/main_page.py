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


def _status_label(status: str) -> str:
    return {"done": "Done", "error": "Failed", "cancelled": "Cancelled"}.get(
        status, status
    )


def _apply_event(run: dict, event: dict) -> None:
    """Fold one streamed event into the in-progress run's rendered state."""
    etype = event.get("type")
    content = event.get("content", "")
    if etype == "messages":
        run["answer"] = content
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
            run["progress"].append(f"✓ {action}")
        elif status not in ("started", None):
            run["progress"].append(f"{action} — failed")
    elif etype == "subgraph_name":
        run["progress"].append(f"Currently at stage: {content}")
    elif etype == "subgraph_message":
        run["progress"].append(f"Thinking: {content}")


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


def _finalize_run(current_chat, chat_id: str, payload: dict) -> None:
    """Retire a finished run and fold its result back into the chat history."""
    st.session_state.active_runs.pop(chat_id, None)
    status = payload["status"]
    if status == "done":
        result = payload.get("result") or {}
        current_chat["history"].append(
            {
                "role": "assistant",
                "content": result.get("answer", ""),
                "progress": result.get("progress") or [],
                "tool_results": result.get("tool_results") or [],
            }
        )
        # Full rerun so the completed answer renders through the history loop
        # (real tables plus the reframe buttons), not the live placeholder.
        st.rerun(scope="app")
    elif status == "error":
        st.error(f"Investigation failed: {payload.get('error') or 'unknown error'}")
    # cancelled: the chat was deleted or the run was stopped; render nothing.


@st.fragment(run_every="1s")
def render_active_run(container, current_chat, token: str, chat_id: str) -> None:
    """Watch the chat's in-flight investigation and render it live.

    A fragment (not the whole script) polls, so a periodic refresh never reruns
    the page; state is keyed by chat id, so navigating away and back re-attaches
    to the same run instead of cancelling it.
    """
    run = st.session_state.active_runs.get(chat_id)
    if not run:
        return
    try:
        payload = chat_client.get_run(
            token=token,
            chat_id=chat_id,
            run_id=run["run_id"],
            since=run["since"],
            wait=0.5,
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

    for event in payload["events"]:
        _apply_event(run, event)
    run["since"] = payload["next_since"]

    status = payload["status"]
    with container:
        stage_idx = [i for i, stage in enumerate(run["progress"]) if "Currently at stage" in stage]
        stage_idx.append(len(run["progress"]))
        for i in range(len(stage_idx) - 2):
            with st.status(
                label=run["progress"][stage_idx[i]],
                type="compact",
                state="complete"
            ):
                for stage in run["progress"][stage_idx[i] + 1 : stage_idx[i + 1]]:
                    st.caption(stage)
        with st.expander(
            label=run["progress"][stage_idx[-2]],
            expanded=True,
            type="compact",
            width="stretch",
        ):
            for stage in run["progress"][stage_idx[-2] + 1 : stage_idx[-1]]:
                st.caption(stage)

    with st.chat_message("assistant"):
        if run["answer"]:
            st.markdown(
                MARKER_RE.sub("_loading table…_", run["answer"])
                + ("▌" if status == "running" else "")
            )
        else:
            st.caption("_thinking…_")

    if status != "running":
        _finalize_run(current_chat, chat_id, payload)


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
                st.session_state.active_runs[chat_id] = {
                    "run_id": run_id,
                    "since": 0,
                    "answer": "",
                    "progress": [],
                    "tool_results": [],
                    "question": None,
                }

    for idx, item in enumerate(current_chat["history"]):
        if (item["role"] == "assistant" and "progress" in item) and item[
            "progress"
        ] is not None:
            stage_idx = [i for i, stage in enumerate(item["progress"]) if "Currently at stage" in stage]
            stage_idx.append(len(item["progress"]))
            for i in range(len(stage_idx) - 1):
                with st.status(
                    label=item["progress"][stage_idx[i]],
                    type="compact",
                    state="complete"
                ):
                    for stage in item["progress"][stage_idx[i] + 1 : stage_idx[i + 1]]:
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
                        for i, target in enumerate(targets):
                            if st.button(
                                ROLE_LABELS[target],
                                key=f"reframe_{target}_{idx}_{i}",
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

    # Live view of an in-flight investigation for this chat (if any).
    if chat_id in st.session_state.active_runs:
        container = st.container()
        render_active_run(container, current_chat, token, chat_id)

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
