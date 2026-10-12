"""Application-data store: authentication and chat persistence."""

from backend.appdb.auth import (
    authenticate,
    bearer_token,
    create_session,
    create_user,
    purge_expired_sessions,
    require_user,
    resolve_session,
    revoke_session,
    verify_password,
)
from backend.appdb.chats import (
    append_message,
    create_chat,
    delete_chat,
    get_messages,
    list_chats,
)

__all__ = [
    "append_message",
    "authenticate",
    "bearer_token",
    "create_chat",
    "create_session",
    "create_user",
    "delete_chat",
    "get_messages",
    "list_chats",
    "purge_expired_sessions",
    "require_user",
    "resolve_session",
    "revoke_session",
    "verify_password",
]
