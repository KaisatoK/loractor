import json

import pyperclip
import streamlit as st

from ui import chat_client
from ui.rendering import MARKER_RE, render_answer
from ui.roles import ROLE_LABELS


@st.fragment(key="copy_to_clipboard_button")
def copy_to_clipboard_button(content: str, role: str, idx: int):
    if st.button(
        label="Copy to Clipboard",
        icon=":material/content_copy:",
        key=f"copy_{role}_{hash(content)}_{idx}",
    ):
        pyperclip.copy(content)
        st.toast(body="Copied to clipboard!", duration=1)


def process_message(current_chat, text, effort):
    current_chat["history"].append(
        {"role": "user", "content": text, "progress": None}
    )
    with st.chat_message("user"):
        st.markdown(text)
    copy_to_clipboard_button(text, "user", len(current_chat["history"]) - 1)

    progress = st.status(label="Processing...", type="compact", state="running")

    full_answer = ""
    progress_stages = []
    tool_results = []
    with st.chat_message("assistant"):
        placeholder = st.empty()

        request_history = [
            {"role": item["role"], "content": item["content"]}
            for item in current_chat["history"]
        ]
        events = chat_client.stream_chat(
            user_id=st.session_state.user_id,
            chat_id=st.session_state.current_chat,
            role=current_chat["role"],
            effort=effort,
            history=request_history,
        )
        for data in events:
            if data["type"] == "messages":
                full_answer = data["content"]
                placeholder.markdown(
                    MARKER_RE.sub("_loading table…_", full_answer) + "▌"
                )
            elif data["type"] == "tool_results":
                tool_results = json.loads(data["content"])
            elif data["type"] == "tool_progress":
                op = json.loads(data["content"])
                action, status = op["operation"], op["status"]
                if status == "started":
                    progress.caption(f"{action[0].upper()}{action[1:]}…")
                elif status == "completed":
                    progress.caption(f"✓ {action}")
                    progress_stages.append(f"✓ {action}")
                else:
                    progress.caption(f"{action} — failed")
                    progress_stages.append(f"{action} — failed")
            elif data["type"] == "subgraph_name":
                progress.caption(f"Currently at stage: {data['content']}")
                progress_stages.append(f"Currently at stage: {data['content']}")
            elif data["type"] == "subgraph_message":
                progress.caption(f"Thinking: {data['content']}")
                progress_stages.append(f"Thinking: {data['content']}")

        progress.update(label="Done", state="complete")
        placeholder.empty()
        render_answer(st, full_answer, tool_results)

    current_chat["history"].append(
        {
            "role": "assistant",
            "content": full_answer,
            "progress": progress_stages,
            "tool_results": tool_results,
        }
    )
    copy_to_clipboard_button(full_answer, "assistant", len(current_chat["history"]) - 1)


def main_page():
    st.title("LangChain Chat Agent")

    current_chat = st.session_state.chat_histories.get(
        st.session_state.current_chat, {}
    )

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
                                        for h in reversed(
                                            current_chat["history"][:idx]
                                        )
                                        if h["role"] == "user"
                                    ),
                                    "",
                                )
                                try:
                                    with st.spinner(
                                        f"Reframing for {ROLE_LABELS[target]}…"
                                    ):
                                        answer = chat_client.reframe(
                                            target_role=target,
                                            effort=st.session_state.get(
                                                "effort", "Medium"
                                            ),
                                            question=question,
                                            previous_answer=item["content"],
                                            tool_results=item.get("tool_results")
                                            or [],
                                        )
                                except Exception as e:
                                    st.error(f"Reframe failed: {e}")
                                else:
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
        process_message(current_chat, message.text, effort)
        # Re-render from history so the new answer immediately gets its reframe
        # buttons/caption (those are only emitted by the history loop above).
        st.rerun()
