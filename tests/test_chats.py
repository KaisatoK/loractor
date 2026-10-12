"""Unit tests for chat and message persistence."""

import pytest

from backend.appdb import auth, chats


def _user(username: str) -> str:
    """Create a real user (chats.user_id has a foreign key to users)."""
    return auth.create_user(username, "password123")["id"]


def test_create_and_list_most_recent_first(app_db):
    user = _user("owner1")
    chats.create_chat(user, "c1", "engineer", title="First")
    chats.create_chat(user, "c2", "manager")
    listed = chats.list_chats(user)
    assert {c["id"] for c in listed} == {"c1", "c2"}
    assert listed[0]["id"] == "c2"  # most recently updated first
    assert listed[0]["role"] == "manager"


def test_create_is_upsert(app_db):
    user = _user("owner2")
    chats.create_chat(user, "c1", "engineer")
    chats.create_chat(user, "c1", "manager", title="Renamed")
    listed = chats.list_chats(user)
    assert len(listed) == 1
    assert listed[0]["role"] == "manager"
    assert listed[0]["title"] == "Renamed"


def test_append_and_get_messages(app_db):
    user = _user("owner3")
    chats.create_chat(user, "c1", "engineer")
    chats.append_message(user, "c1", "user", "hi")
    chats.append_message(
        user,
        "c1",
        "assistant",
        "yo",
        tool_results=[{"id": "R1"}],
        progress=["stage"],
    )
    messages = chats.get_messages(user, "c1")
    assert [m["content"] for m in messages] == ["hi", "yo"]
    assert messages[1]["tool_results"] == [{"id": "R1"}]
    assert messages[1]["progress"] == ["stage"]
    assert messages[0]["tool_results"] is None
    assert messages[1]["reframed_for"] is None


def test_append_reframed_message(app_db):
    user = _user("owner4")
    chats.create_chat(user, "c1", "engineer")
    chats.append_message(user, "c1", "assistant", "x", reframed_for="manager")
    assert chats.get_messages(user, "c1")[0]["reframed_for"] == "manager"


def test_append_to_missing_chat_raises(app_db):
    user = _user("owner5")
    with pytest.raises(KeyError):
        chats.append_message(user, "missing", "user", "x")


def test_delete_cascades_and_is_idempotent(app_db):
    user = _user("owner6")
    chats.create_chat(user, "c1", "engineer")
    chats.append_message(user, "c1", "user", "hi")
    assert chats.delete_chat(user, "c1") is True
    assert chats.list_chats(user) == []
    assert chats.get_messages(user, "c1") == []
    assert chats.delete_chat(user, "c1") is False


def test_user_isolation(app_db):
    owner = _user("owner7")
    intruder = _user("intruder7")
    chats.create_chat(owner, "c1", "engineer")
    chats.append_message(owner, "c1", "user", "secret")

    assert chats.list_chats(intruder) == []
    assert chats.get_messages(intruder, "c1") == []
    assert chats.delete_chat(intruder, "c1") is False
    assert chats.get_messages(owner, "c1")[0]["content"] == "secret"
