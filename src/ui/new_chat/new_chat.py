from datetime import datetime
from pathlib import Path
from string import Template

import streamlit as st

from ui import chat_client
from ui.roles import ROLES

ASSETS = Path(__file__).parent / "assets"


def read_asset(name: str) -> str:
    return (ASSETS / name).read_text(encoding="utf-8")


def load_css() -> None:
    st.markdown(f"<style>{read_asset('style.css')}</style>", unsafe_allow_html=True)


def new_chat():
    load_css()
    st.html(read_asset("hello.html"))

    def start_chat(role: str):
        new_chat_id = datetime.now().strftime("%Y%m%d%H%M%S")
        try:
            chat_client.create_chat(
                st.session_state.auth["token"], new_chat_id, role
            )
        except Exception as err:  # noqa: BLE001 - surface to the user
            st.error(f"Could not start a new chat: {err}")
            return
        st.session_state.chat_histories[new_chat_id] = {
            "role": role,
            "history": [],
        }
        st.session_state.current_chat = new_chat_id
        st.rerun()

    row_template = Template(read_asset("role_row.html"))

    for role in ROLES:
        with st.container(key=f"row_{role['id']}"):
            text_col, button_col = st.columns([3, 1], vertical_alignment="center")
            with text_col:
                st.html(
                    row_template.substitute(
                        title=role["title"], description=role["description"]
                    )
                )
            with button_col:
                if st.button(
                    "Start chat",
                    key=f"start_{role['id']}",
                    help=f"Start a new chat as {role['title']}",
                    width="stretch",
                ):
                    start_chat(role["id"])

    st.chat_input("Choose a role to start a chat", disabled=True)
