import json
from datetime import datetime

import pyperclip
import streamlit as st

from ui import chat_client
from ui.chat_box import chat_box
from ui.rendering import MARKER_RE, render_answer
from ui.roles import ROLE_LABELS


def _parse_ts(value) -> datetime | None:
    """Parse a stored ISO-8601 message timestamp, or ``None`` if unusable."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, rest = divmod(seconds, 60)
    return f"{int(minutes)}m {rest:04.1f}s"


def _answer_elapsed(history: list[dict], idx: int, item: dict) -> float | None:
    """Seconds from receiving the user's question to this answer being ready.

    Freshly finished runs carry the worker's own monotonic measurement
    (``elapsed_seconds``). For history loaded from the backend it is derived from
    the stored ``created_at`` of the question and of the answer — the user turn
    is persisted when the investigation is accepted and the assistant turn when
    it completes, so the difference is the same window.
    """
    live = item.get("elapsed_seconds")
    if live is not None:
        return float(live)
    if item.get("reframed_for"):
        # Reframing reuses existing evidence; timing it against the original
        # question would overstate the work done.
        return None
    answered = _parse_ts(item.get("created_at"))
    if answered is None:
        return None
    asked = next(
        (
            _parse_ts(h.get("created_at"))
            for h in reversed(history[:idx])
            if h.get("role") == "user"
        ),
        None,
    )
    if asked is None:
        return None
    return max(0.0, (answered - asked).total_seconds())


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
        elif status == "working":
            # A long-running tool reporting from inside itself. The sweep
            # iterates over up to 1,624 chunks in one call, so these are the
            # only sign it is progressing; the message says which pass it is on
            # and what it decided, so it is shown instead of the operation name.
            run["progress"].append(
                f"  → {op.get('message') or action}"
            )
        elif status == "skipped":
            # A refusal, not a failure: the query was never run because it was
            # unbounded or unsupported as asked, and it came back with a
            # suggestion. Labelling it "failed" misreads a useful answer as a
            # broken tool.
            run["progress"].append(f"↷ {op.get('message') or action}")
        elif status not in ("started", None):
            run["progress"].append(f"{action} — failed")
    elif etype == "subgraph_name":
        run["progress"].append(f"Currently at stage: {content}")
    elif etype == "stage":
        run["progress"].append(f"Currently at stage: {content}")
    elif etype == "thinking_delta":
        key = event.get("id") or ""
        if run.get("thinking_key") != key:
            run["progress"].append("Thinking: ")
            run["thinking_key"] = key
            run["thinking_at"] = len(run["progress"]) - 1
        run["progress"][run["thinking_at"]] += content
    elif etype == "subgraph_message":
        # Consolidated copy of the thought; the backend already keeps it in the
        # run's persisted progress. Arriving here closes the open live thought so
        # the next LLM call starts a new line.
        run["thinking_key"] = None


def _annotate_last_user_turn(current_chat, **flags) -> None:
    """Flag the user turn that started the run that just ended without an answer.

    This is a flag on the existing history item, never a new entry: local history
    mirrors the persisted messages one-to-one (answers are numbered
    ``len(history) + 1``) and is what the next request sends to the backend.
    """
    for item in reversed(current_chat.get("history") or []):
        if item["role"] == "user":
            item.update(flags)
            return


def _finalize_run(current_chat, chat_id: str, payload: dict) -> None:
    """Retire a finished run and fold its result back into the chat history."""
    st.session_state.active_runs.pop(chat_id, None)
    status = payload["status"]
    if status == "done":
        result = payload.get("result") or {}
        current_chat["history"].append(
            {
                # Server seq is 1-based and contiguous per chat, and every local
                # history item mirrors a persisted message.
                "seq": len(current_chat["history"]) + 1,
                "role": "assistant",
                "content": result.get("answer", ""),
                "progress": result.get("progress") or [],
                "tool_results": result.get("tool_results") or [],
                "elapsed_seconds": result.get("elapsed_seconds"),
            }
        )
        # Full rerun so the completed answer renders through the history loop
        # (real tables plus the reframe buttons), not the live placeholder.
        st.rerun(scope="app")
        return
    if status == "error":
        _annotate_last_user_turn(
            current_chat, run_error=payload.get("error") or "unknown error"
        )
    elif status == "cancelled":
        _annotate_last_user_turn(current_chat, stopped=True)
    # The chat box stays locked while a run is active, so a full rerun is what
    # unlocks it; the outcome is drawn under the question by the history loop.
    st.rerun(scope="app")


@st.fragment(run_every="0.5s")
def render_active_run(container, current_chat, token: str, chat_id: str) -> None:
    """Watch the chat's in-flight investigation and render it live.
    A fragment (not the whole script) polls, so refreshes never rerun the page;
    state keyed by chat id re-attaches on navigation instead of cancelling."""
    run = st.session_state.active_runs.get(chat_id)
    if not run:
        return
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

    for event in payload["events"]:
        _apply_event(run, event)
    run["since"] = payload["next_since"]

    status = payload["status"]
    with container:
        stage_idx = [i for i, stage in enumerate(run["progress"]) if "Currently at stage" in stage]
        stage_idx.append(len(run["progress"]))
        # No stage may have started yet (normalize/scope no longer surface a
        # subgraph), so the marker list can be empty; "Processing..." is the
        # label this panel used before it was stage-grouped.
        markers = stage_idx[:-1]
        for i in range(len(markers) - 1):
            with st.status(
                label=run["progress"][markers[i]],
                type="compact",
                state="complete"
            ):
                for stage in run["progress"][markers[i] + 1 : markers[i + 1]]:
                    st.caption(stage)
        with st.expander(
            label=run["progress"][markers[-1]] if markers else "Processing...",
            expanded=True,
            type="compact",
            width="stretch",
        ):
            for stage in run["progress"][(markers[-1] + 1) if markers else 0 : stage_idx[-1]]:
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



# --- Role switch ---------------------------------------------------------------


def _load_role_answers(token: str, chat_id: str) -> dict:
    """``"seq:role" -> answer`` for every cached per-role answer of a chat."""
    try:
        answers = chat_client.list_role_answers(token, chat_id)
    except Exception:  # noqa: BLE001 - role answers are an optional extra
        return {}
    return {f"{a['message_seq']}:{a['role']}": a for a in answers}


def _role_run_key(chat_id: str, seq: int, role: str) -> str:
    return f"{chat_id}|{seq}|{role}"


def _role_runs() -> dict:
    return st.session_state.setdefault("role_runs", {})


def _open_roles() -> dict:
    return st.session_state.setdefault("role_open", {})


def render_role_switch(token: str, chat_id: str, current_chat: dict, seq: int) -> None:
    """Role buttons under an answer, plus that answer's other-role versions.

    A click asks the backend for the answer as that role: a cached answer shows
    at once; otherwise a new role-specific investigation runs in the background
    and is cached, so switching back and forth never re-runs it.
    """
    answers = current_chat.setdefault("role_answers", {})
    own_role = current_chat["role"]
    targets = [t for t in ROLE_LABELS if t != own_role]
    with st.container(horizontal=True, horizontal_alignment="left"):
        for target in targets:
            cached = f"{seq}:{target}" in answers
            label = f"As {ROLE_LABELS[target]}" + (" ✓" if cached else "")
            if st.button(label, key=f"role_{chat_id}_{seq}_{target}", width="content"):
                _request_role(token, chat_id, current_chat, seq, target)

    for target in targets:
        key = _role_run_key(chat_id, seq, target)
        if key in _role_runs():
            render_role_run(token, chat_id, current_chat, seq, target)
        answer = answers.get(f"{seq}:{target}")
        if answer:
            with st.expander(
                f"Answer for {ROLE_LABELS[target]} — separate investigation "
                f"({answer.get('effort')} effort)",
                expanded=_open_roles().get(f"{chat_id}|{seq}") == target,
            ):
                render_answer(st, answer["content"], answer.get("tool_results"))


def _request_role(token: str, chat_id: str, current_chat: dict, seq: int, role: str) -> None:
    effort = st.session_state.get("effort", "Medium")
    try:
        response = chat_client.request_role_answer(
            token=token, chat_id=chat_id, message_seq=seq, role=role, effort=effort
        )
    except Exception as err:  # noqa: BLE001
        st.error(f"Could not answer as {ROLE_LABELS[role]}: {err}")
        return
    _open_roles()[f"{chat_id}|{seq}"] = role
    if response.get("status") == "cached":
        current_chat.setdefault("role_answers", {})[f"{seq}:{role}"] = response["answer"]
    else:
        _role_runs()[_role_run_key(chat_id, seq, role)] = {
            "effort": effort,
            "run_id": response["run_id"],
            "since": 0,
            "answer": "",
            "progress": [],
            "tool_results": [],
        }
    st.rerun()


@st.fragment(run_every="1s")
def render_role_run(token: str, chat_id: str, current_chat: dict, seq: int, role: str) -> None:
    """Poll one in-flight role-switch run and fold it into the cache when done."""
    key = _role_run_key(chat_id, seq, role)
    run = _role_runs().get(key)
    if not run:
        return
    try:
        payload = chat_client.get_run(
            token=token, chat_id=chat_id, run_id=run["run_id"], since=run["since"]
        )
    except Exception as err:  # noqa: BLE001
        if getattr(getattr(err, "response", None), "status_code", None) == 404:
            _role_runs().pop(key, None)
            current_chat["role_answers"] = _load_role_answers(token, chat_id)
            st.rerun(scope="app")
        return
    for event in payload["events"]:
        _apply_event(run, event)
    run["since"] = payload["next_since"]

    status = payload["status"]
    if status == "running":
        stages = [p for p in run["progress"] if p.startswith("Currently at stage")]
        st.caption(
            f"Investigating as {ROLE_LABELS[role]}… "
            + (stages[-1].removeprefix("Currently at stage: ") if stages else "")
        )
        return
    _role_runs().pop(key, None)
    if status == "done":
        result = payload.get("result") or {}
        current_chat.setdefault("role_answers", {})[f"{seq}:{role}"] = {
            "message_seq": seq,
            "role": role,
            "effort": run.get("effort"),
            "content": result.get("answer", ""),
            "tool_results": result.get("tool_results") or [],
            "progress": result.get("progress") or [],
        }
    elif status == "error":
        st.error(f"{ROLE_LABELS[role]} investigation failed: {payload.get('error')}")
        return
    st.rerun(scope="app")


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
        current_chat["role_answers"] = _load_role_answers(token, chat_id)

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
                elapsed = _answer_elapsed(current_chat["history"], idx, item)
                if elapsed is not None:
                    st.caption(f"Answered in {_format_duration(elapsed)}")
                if item.get("tool_results") and not item.get("reframed_for"):
                    # Answer the same question as another role: a separate,
                    # role-specific investigation, cached per (message, role).
                    render_role_switch(
                        token, chat_id, current_chat, item.get("seq") or idx + 1
                    )
            else:
                st.markdown(item["content"])
                if item.get("stopped"):
                    st.caption(":material/stop_circle: Stopped by user")
                if item.get("run_error"):
                    st.error(f"Investigation failed: {item['run_error']}")
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

    with col2:
        chat_box(token, current_chat, chat_id, effort)
