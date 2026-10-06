import streamlit as st
from datetime import datetime


def new_chat_page():
    st.title("Hello there! 👋")

    def new_chat(role: str):
        new_chat_id = datetime.now().strftime("%Y%m%d%H%M%S")
        st.session_state.chat_histories[new_chat_id] = {
            "role": role,
            "history": [],
        }
        st.session_state.current_chat = new_chat_id
        st.rerun()

    left, middle, right = st.columns(3)

    if left.button("Engineer", width="stretch"):
        new_chat("engineer")
    if middle.button("Team Lead", width="stretch"):
        new_chat("team_lead")
    if right.button("Manager", width="stretch"):
        new_chat("manager")
