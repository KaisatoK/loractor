import uuid
import requests
import streamlit as st

API_URL = "http://localhost:8000/chat/stream"


st.title("LangChain Ollama Chat Agent")

if "user_id" not in st.session_state:
    st.session_state.user_id = str(uuid.uuid4())

if "chat_history" not in st.session_state:
    st.session_state.chat_history = []

for item in st.session_state.chat_history:
    with st.chat_message(item["role"]):
        st.markdown(item["content"])

message = st.chat_input("Type your message here...")

if message:
    st.session_state.chat_history.append({"role": "user", "content": message})
    with st.chat_message("user"):
        st.markdown(message)

    full_answer = ""
    with st.chat_message("assistant"):
        placeholder = st.empty()

        with requests.post(
            API_URL,
            json={
                "user_id": st.session_state.user_id,
                "message": message,
            },
            stream=True,
        ) as response:
            response.raise_for_status()

            for chunk in response.iter_content(chunk_size=None, decode_unicode=True):
                if chunk:
                    full_answer += chunk
                    placeholder.markdown(full_answer + "▌")

        placeholder.markdown(full_answer)

    st.session_state.chat_history.append({"role": "assistant", "content": full_answer})

