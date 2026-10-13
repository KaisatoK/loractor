"""Login / registration page shown before the chat UI is available."""

import requests
import streamlit as st

from ui import chat_client


def _error_detail(err: requests.HTTPError) -> str:
    response = getattr(err, "response", None)
    if response is None:
        return str(err)
    try:
        detail = response.json().get("detail")
    except Exception:
        return f"Request failed ({response.status_code})."
    if isinstance(detail, list):
        # FastAPI validation errors: [{"msg": ..., "loc": [...]}, ...]
        messages = [str(item.get("msg", item)) for item in detail]
        return "; ".join(messages) or f"Request failed ({response.status_code})."
    return str(detail) if detail else f"Request failed ({response.status_code})."


def sign_in(call) -> None:
    try:
        data = call()
    except requests.HTTPError as err:
        st.error(_error_detail(err))
    except requests.RequestException as err:
        st.error(f"Could not reach the backend: {err}")
    else:
        st.session_state.auth = {
            "token": data["token"],
            "user": data["user"],
            "expires_at": data.get("expires_at"),
        }
        # Force a fresh chat list for the newly authenticated user.
        st.session_state.chats_loaded = False
        st.rerun()


def login_page() -> None:
    st.title("LangChain Chat Agent")
    st.caption("Sign in to investigate your logs.")

    sign_in_tab, register_tab = st.tabs(["Sign in", "Create account"])

    with sign_in_tab:
        with st.form("login_form"):
            username = st.text_input("Username")
            password = st.text_input("Password", type="password")
            submitted = st.form_submit_button("Sign in", type="primary")
        if submitted:
            if not username or not password:
                st.error("Enter your username and password.")
            else:
                sign_in(lambda: chat_client.login(username, password))

    with register_tab:
        with st.form("register_form"):
            new_username = st.text_input("Username", key="reg_username")
            new_password = st.text_input(
                "Password", type="password", key="reg_password"
            )
            confirm = st.text_input(
                "Confirm password", type="password", key="reg_confirm"
            )
            registered = st.form_submit_button("Create account", type="primary")
        if registered:
            if new_password != confirm:
                st.error("Passwords do not match.")
            else:
                sign_in(lambda: chat_client.register(new_username, new_password))
