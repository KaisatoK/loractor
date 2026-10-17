"""Offline UI tests for the role-switch buttons and the per-role answer cache.

``main_page`` runs for real in Streamlit's AppTest; the backend client calls are
stubbed, so no server, database or model is needed.
"""

from streamlit.testing.v1 import AppTest

_SCRIPT = '''
import streamlit as st
from ui import chat_client
from ui.main_page.main_page import main_page

calls = st.session_state.setdefault("calls", [])

def stub_request(**kw):
    calls.append(kw)
    if kw["role"] == "manager":
        return {"status": "cached", "answer": {
            "message_seq": kw["message_seq"], "role": "manager", "effort": "Medium",
            "content": "MANAGER PRIORITIES", "tool_results": []}}
    return {"status": "running", "run_id": "run-tl"}

def stub_get_run(**kw):
    return {"events": [{"type": "messages", "content": "TEAM LEAD PATTERNS"}],
            "next_since": 1, "status": "done",
            "result": {"answer": "TEAM LEAD PATTERNS", "tool_results": [], "progress": []}}

chat_client.request_role_answer = stub_request
chat_client.get_run = stub_get_run
chat_client.list_role_answers = lambda token, chat_id: []
st.session_state.setdefault("auth", {"token": "tok", "user": {"username": "alice"}})
st.session_state.setdefault("current_chat", "chat-1")
st.session_state.setdefault("active_runs", {})
st.session_state.setdefault("chat_histories", {"chat-1": {"role": "engineer", "history": [
    {"seq": 1, "role": "user", "content": "Why do spawns fail?", "progress": None},
    {"seq": 2, "role": "assistant", "content": "ENGINEER ANSWER", "progress": [],
     "tool_results": [{"id": "R1", "rows": [{"a": 1}]}]},
], "role_answers": {}}})
main_page()
'''


def _button(at, name):
    return next(b for b in at.button if b.label.startswith("As ") and name in b.label)


def test_role_buttons_offer_other_roles_only():
    at = AppTest.from_string(_SCRIPT).run(timeout=30)
    assert not at.exception
    labels = [b.label for b in at.button if b.label.startswith("As ")]
    assert len(labels) == 2
    assert "Team Lead" in labels[0] and "Manager" in labels[1]
    assert not any("Engineer" in label for label in labels)


def test_cached_role_answer_shows_without_new_run():
    at = AppTest.from_string(_SCRIPT).run(timeout=30)
    _button(at, "Manager").click().run(timeout=30)
    assert not at.exception
    assert any(e.label.startswith("Answer for") and "Manager" in e.label for e in at.expander)
    assert any("MANAGER PRIORITIES" in m.value for m in at.markdown)
    # The button now marks the role as cached.
    assert _button(at, "Manager").label.endswith("✓")
    call = at.session_state["calls"][-1]
    assert (call["message_seq"], call["role"]) == (2, "manager")


def test_uncached_role_runs_then_is_cached():
    at = AppTest.from_string(_SCRIPT).run(timeout=30)
    _button(at, "Team Lead").click().run(timeout=30)
    at.run(timeout=30)  # the polling fragment folds the finished run in
    assert not at.exception
    assert at.session_state["role_runs"] == {}
    answers = at.session_state["chat_histories"]["chat-1"]["role_answers"]
    assert answers["2:team_lead"]["content"] == "TEAM LEAD PATTERNS"
    assert _button(at, "Team Lead").label.endswith("✓")
