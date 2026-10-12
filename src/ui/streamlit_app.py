import streamlit as st

from ui import chat_client
from ui.login.login import login_page
from ui.main_page.main_page import main_page
from ui.new_chat.new_chat import new_chat

st.set_page_config(
    page_title="LangChain Chat Agent", page_icon=":robot_face:", layout="wide"
)

# --- Authentication gate ------------------------------------------------------

if "auth" not in st.session_state:
    st.session_state.auth = None

if not st.session_state.auth:
    login_page()
    st.stop()

token: str = st.session_state.auth["token"]
username: str = st.session_state.auth["user"]["username"]

# Load the user's chats from the backend once per login. Histories are fetched
# lazily (history=None) when a chat is opened.
if not st.session_state.get("chats_loaded"):
    try:
        chats = chat_client.list_chats(token)
    except Exception as err:  # noqa: BLE001 - surface to the user
        st.session_state.chat_histories = {}
        st.error(f"Could not load your chats: {err}")
    else:
        st.session_state.chat_histories = {
            chat["id"]: {"role": chat["role"], "history": None} for chat in chats
        }
    st.session_state.chats_loaded = True

if "chat_histories" not in st.session_state:
    st.session_state.chat_histories = {}
if "current_chat" not in st.session_state:
    st.session_state.current_chat = None


@st.dialog("Are you sure?")
def delete(chat_id: str):
    st.markdown(f"Are you sure you want to delete chat {chat_id}?")
    if st.button("Yes, delete", type="primary"):
        try:
            chat_client.delete_chat(token, chat_id)
        except Exception as err:  # noqa: BLE001
            st.error(f"Could not delete chat: {err}")
        else:
            st.session_state.chat_histories.pop(chat_id, None)
            if st.session_state.current_chat == chat_id:
                st.session_state.current_chat = None
            st.rerun()


def _logout() -> None:
    try:
        chat_client.logout(token)
    except Exception:  # noqa: BLE001 - logging out should never fail loudly
        pass
    for key in ("auth", "chat_histories", "current_chat", "chats_loaded", "effort"):
        st.session_state.pop(key, None)
    st.rerun()


# --- Sidebar navigation -------------------------------------------------------

with st.sidebar:
    st.title("Conversations")
    st.caption(f"Signed in as **{username}**")
    if st.button("Log out", width="stretch"):
        _logout()
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
