import uuid
import streamlit as st

from main_page.main_page import main_page
from new_chat.new_chat import new_chat


@st.dialog("Are you sure?")
def delete(chat_id: int):
    st.markdown(f"Are you sure you want to delete chat {chat_id}?")
    if st.button("Yes, delete", type="primary"):
        del st.session_state.chat_histories[chat_id]
        if st.session_state.current_chat == chat_id:
            st.session_state.current_chat = None
        st.rerun()


st.set_page_config(
    page_title="LangChain Chat Agent", page_icon=":robot_face:", layout="wide"
)

if "user_id" not in st.session_state:
    st.session_state.user_id = str(uuid.uuid4())

# TODO: Implement save/load chat histories to/from a file or database, because currently, the chat histories will be lost when the app is restarted or refreshed.
if "chat_histories" not in st.session_state:
    st.session_state.chat_histories = {}

# Initialize sidebar navigation
with st.sidebar:
    st.title("Conversations")
    if st.button("New Chat", type="secondary", width="stretch"):
        st.session_state.current_chat = None
        st.rerun()
    st.divider()
    for chat_id, chat_data in reversed(list(st.session_state.chat_histories.items())):
        col1, col2 = st.columns([0.8, 0.2], gap="xxsmall")
        if col2.button(
            "🗑️", key=f"delete_{chat_id}", type="secondary", width="stretch"
        ):
            delete(chat_id)
        if col1.button(
            f"{chat_data['role'].capitalize()} - {chat_id}",
            type="secondary",
            width="stretch",
        ):
            st.session_state.current_chat = chat_id
            st.rerun()

if ("current_chat" not in st.session_state) or (
    st.session_state.current_chat not in st.session_state.chat_histories
):
    new_chat()
else:
    main_page()
