import json
import re
import pandas as pd
import pyperclip
import requests
import streamlit as st

API_URL = "http://localhost:8000/chat/stream"
# Canonical marker is [[RESULT:R1]], but models sometimes emit variants such as
# [RESULT:R1], 【RESULT:R1】, ［RESULT:R1］, or 【R1】 with CJK/fullwidth brackets.
# Accept all of them so a table still renders and no raw marker leaks into the UI.
MARKER_RE = re.compile(
    r"[\[【［]{1,2}\s*(?:RESULT\s*:\s*)?(R\d+)\s*[\]】］]{1,2}"
)
# Sentence/clause punctuation that belongs to the text preceding a marker.
TRAILING_PUNCT = ".,;:!?"


def render_answer(container, text, results):
    by_id = {r["id"]: r for r in (results or [])}
    pos = 0
    for match in MARKER_RE.finditer(text):
        # Punctuation right after a marker (e.g. "[[RESULT:R1]].") belongs to the
        # preceding sentence. Move it before the marker so it does not render on
        # its own line underneath the table.
        end = match.end()
        trailing = ""
        while end < len(text) and text[end] in TRAILING_PUNCT:
            trailing += text[end]
            end += 1
        chunk = text[pos : match.start()].rstrip() + trailing
        if chunk.strip():
            container.markdown(chunk)
        record = by_id.get(match.group(1))
        if record and record.get("rows"):
            container.dataframe(
                pd.DataFrame(record["rows"]),
                width="stretch",
                hide_index=True,
            )
        # unknown id → render nothing (guards against hallucinated markers)
        pos = end
    tail = text[pos:]
    if tail.strip():
        container.markdown(tail)


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
    copy_to_clipboard_button(text, "user", len(current_chat['history']) - 1)

    progress = st.status(label="Processing...", type="compact", state="running")

    full_answer = ""
    progress_stages = []
    tool_results = []
    with st.chat_message("assistant"):
        placeholder = st.empty()

        with requests.post(
            API_URL,
            json={
                "user_id": st.session_state.user_id,
                "role": current_chat["role"],
                "effort": effort,
                "message": str([{"role": item["role"], "content": item["content"]} for item in current_chat["history"]]),
            },
            stream=True,
        ) as response:
            response.raise_for_status()

            for chunk in response.iter_content(
                chunk_size=None, decode_unicode=True
            ):
                if chunk:
                    data = (
                        json.loads(chunk.decode("utf-8"))
                        if isinstance(chunk, bytes)
                        else json.loads(chunk)
                    )
                    if data["type"] == "messages":
                        full_answer = data["content"]
                        placeholder.markdown(
                            MARKER_RE.sub("_loading table…_", full_answer) + "▌"
                        )
                    elif data["type"] == "tool_results":
                        tool_results = json.loads(data["content"])
                    elif data["type"] == "tool_progress":
                        op = json.loads(data["content"])
                        progress.caption(f"🛠 {op['operation']} — {op['status']}")
                        if op["status"] in ("completed", "failed"):
                            progress_stages.append(f"🛠 {op['operation']} — {op['status']}")
                    elif data["type"] == "subgraph_name":
                        progress.caption(f"Currently at stage: {data['content']}")
                        progress_stages.append(f"Currently at stage: {data['content']}")
                    elif data["type"] == "subgraph_message":
                        progress.caption(f"Thinking: {data['content']}")
                        progress_stages.append(f"Thinking: {data['content']}")
                # time.sleep(0.1)  # Simulate processing time for better UX

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
    copy_to_clipboard_button(full_answer, "assistant", len(current_chat['history']) - 1)


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
                render_answer(st, item["content"], item.get("tool_results"))
            else:
                st.markdown(item["content"])
        copy_to_clipboard_button(item['content'], item['role'], idx)

    col1, col2 = st.bottom.columns(spec=[0.1, 0.9], vertical_alignment="bottom")

    effort = col1.selectbox(
        "Effort Level",
        ["Low", "Medium", "High", "Extreme", "Brutal"],
        index=1,
        help="Low: quick check — fewer tool calls, 1 hypothesis. Brutal: deepest investigation.",
    )

    message = col2.chat_input(
        placeholder="Type your message here...", accept_file="multiple"
    )

    if message:
        process_message(current_chat, message.text, effort)
