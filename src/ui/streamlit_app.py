import uuid
from datetime import datetime
from new_chat import new_chat_page
from main_page import main_page
import streamlit as st

st.set_page_config(
    page_title="LangChain Ollama Chat Agent", page_icon=":robot_face:", layout="wide"
)

if "user_id" not in st.session_state:
    st.session_state.user_id = str(uuid.uuid4())

# TODO: Implement save/load chat histories to/from a file or database, because currently, the chat histories will be lost when the app is restarted or refreshed.
if "chat_histories" not in st.session_state:
    st.session_state.chat_histories = {}

# # Initialize sidebar navigation
# pages = {
#     "New Chat": [st.Page(new_chat_page, title="New Chat")],
#     "Conversations": []
# }

# for chat_id, chat_data in st.session_state.chat_histories.items():
#     def navigate_to_chat():
#         st.session_state.current_chat = chat_id
#         main_page()

#     pages["Conversations"].append(
#         st.Page(
#             navigate_to_chat,
#             title=f"{chat_data['role'].capitalize()} Chat - {chat_id}"
#         )
#     )

# pg = st.navigation(pages)
# pg.run()

# if ("current_chat" not in st.session_state):
#     new_chat_page()

if ("current_chat" not in st.session_state) or (
    st.session_state.current_chat not in st.session_state.chat_histories
):
    new_chat_page()
else:
    main_page()
