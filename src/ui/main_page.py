import requests
import json
import streamlit as st

API_URL = "http://localhost:8000/chat/stream"


def main_page():
    st.title("LangChain Chat Agent")

    current_chat = st.session_state.chat_histories.get(
        st.session_state.current_chat, {}
    )

    for item in current_chat["history"]:
        if (item["role"] == "assistant" and "progress" in item) and item[
            "progress"
        ] is not None:
            with st.status(label="Done", type="compact", state="complete"):
                for stage in item["progress"]:
                    st.write(f"Currently at: {stage}")
        with st.chat_message(item["role"]):
            st.markdown(item["content"])

    message = st.chat_input(
        placeholder="Type your message here...", accept_file="multiple"
    )

    if message:
        current_chat["history"].append(
            {"role": "user", "content": message.text, "progress": None}
        )
        with st.chat_message("user"):
            st.markdown(message.text)

        progress = st.status(label="Processing...", type="compact", state="running")

        full_answer = ""
        progress_stages = []
        with st.chat_message("assistant"):
            placeholder = st.empty()

            with requests.post(
                API_URL,
                json={
                    "user_id"   : st.session_state.user_id,
                    "role"      : current_chat["role"],
                    "message"   : str(current_chat["history"]),
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
                            full_answer += data["content"]
                            placeholder.markdown(full_answer + "▌")
                        elif data["type"] == "subgraphs":
                            progress.write(f"Currently at: {data['content']}")
                            progress_stages.append(data["content"])
                    # time.sleep(0.1)  # Simulate processing time for better UX

            progress.update(label="Done", state="complete")
            placeholder.markdown(full_answer)

        current_chat["history"].append(
            {"role": "assistant", "content": full_answer, "progress": progress_stages}
        )
