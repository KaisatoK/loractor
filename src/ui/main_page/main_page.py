import json
import pyperclip
import requests
import streamlit as st

API_URL = "http://localhost:8000/chat/stream"

@st.fragment(key="copy_to_clipboard_button")
def copy_to_clipboard_button(content: str, role: str, idx: int):
    if st.button(
        label="Copy to Clipboard",
        icon=":material/content_copy:",
        key=f"copy_{role}_{hash(content)}_{idx}",
    ):
        pyperclip.copy(content)
        st.toast(body="Copied to clipboard!", duration=1)

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
            st.markdown(item["content"])
        copy_to_clipboard_button(item['content'], item['role'], idx)

    col1, col2 = st.bottom.columns(spec=[0.1, 0.9], vertical_alignment="bottom")

    effort = col1.selectbox(
        "Effort Level", ["Low", "Medium", "High", "Extreme", "Brutal"], index=1
    )

    message = col2.chat_input(
        placeholder="Type your message here...", accept_file="multiple"
    )

    if message:
        current_chat["history"].append(
            {"role": "user", "content": message.text, "progress": None}
        )
        with st.chat_message("user"):
            st.markdown(message.text)
        copy_to_clipboard_button(message.text, "user", len(current_chat['history']) - 1)

        progress = st.status(label="Processing...", type="compact", state="running")

        full_answer = ""
        progress_stages = []
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
                            placeholder.markdown(full_answer + "▌")
                        elif data["type"] == "subgraph_name":
                            progress.caption(f"Currently at stage: {data['content']}")
                            progress_stages.append(f"Currently at stage: {data['content']}")
                        elif data["type"] == "subgraph_message":
                            progress.caption(f"Thinking: {data['content']}")
                            progress_stages.append(f"Thinking: {data['content']}")
                    # time.sleep(0.1)  # Simulate processing time for better UX

            progress.update(label="Done", state="complete")
            placeholder.markdown(full_answer)

        current_chat["history"].append(
            {"role": "assistant", "content": full_answer, "progress": progress_stages}
        )
        copy_to_clipboard_button(full_answer, "assistant", len(current_chat['history']) - 1)
