"""Unit tests for the authentication store."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from backend.appdb import auth
from backend.appdb.database import db


def test_hash_and_verify_roundtrip():
    hashed = auth.hash_password("s3cret-password")
    assert hashed.startswith("scrypt$")
    assert auth.verify_password("s3cret-password", hashed)
    assert not auth.verify_password("wrong-password", hashed)


def test_verify_rejects_malformed_hash():
    assert not auth.verify_password("x", "not-a-hash")
    assert not auth.verify_password("x", "bcrypt$1$2$3$4$5")
    assert not auth.verify_password("x", "scrypt$a$b$c$d$e")


def test_salt_is_unique_per_hash():
    assert auth.hash_password("same-password") != auth.hash_password("same-password")


def test_create_and_authenticate(app_db):
    user = auth.create_user("alice", "password123")
    assert user["username"] == "alice"
    assert auth.authenticate("alice", "password123")["id"] == user["id"]
    assert auth.authenticate("alice", "wrong") is None
    assert auth.authenticate("ghost", "password123") is None


def test_duplicate_username_rejected_case_insensitively(app_db):
    auth.create_user("bob", "password123")
    with pytest.raises(ValueError):
        auth.create_user("BOB", "password123")


def test_session_lifecycle(app_db):
    user = auth.create_user("carol", "password123")
    token, expires_at = auth.create_session(user["id"])
    assert expires_at
    assert auth.resolve_session(token)["username"] == "carol"
    auth.revoke_session(token)
    assert auth.resolve_session(token) is None


def test_unknown_token_resolves_to_none(app_db):
    assert auth.resolve_session("does-not-exist") is None


def test_expired_session_is_rejected_and_deleted(app_db):
    user = auth.create_user("dave", "password123")
    token, _ = auth.create_session(user["id"])
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    with db() as cur:
        cur.execute(
            "UPDATE sessions SET expires_at = ? WHERE user_id = ?", (past, user["id"])
        )

    assert auth.resolve_session(token) is None
    with db() as cur:
        remaining = cur.execute(
            "SELECT COUNT(*) FROM sessions WHERE user_id = ?", (user["id"],)
        ).fetchone()[0]
    assert remaining == 0


def test_purge_expired_sessions(app_db):
    user = auth.create_user("erin", "password123")
    token, _ = auth.create_session(user["id"])
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    with db() as cur:
        cur.execute("UPDATE sessions SET expires_at = ?", (past,))

    auth.purge_expired_sessions()
    assert auth.resolve_session(token) is None


def test_bearer_token_dependency():
    with pytest.raises(HTTPException) as missing:
        auth.bearer_token(None)
    assert missing.value.status_code == 401

    with pytest.raises(HTTPException):
        auth.bearer_token("Token abc")

    assert auth.bearer_token("Bearer abc") == "abc"
    assert auth.bearer_token("bearer abc") == "abc"


def test_require_user_dependency(app_db):
    user = auth.create_user("frank", "password123")
    token, _ = auth.create_session(user["id"])
    assert auth.require_user(token)["username"] == "frank"

    with pytest.raises(HTTPException) as invalid:
        auth.require_user("bogus-token")
    assert invalid.value.status_code == 401
