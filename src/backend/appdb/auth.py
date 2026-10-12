"""User accounts, password hashing, and bearer-session authentication.

Passwords are hashed with scrypt (stdlib only). Sessions are opaque random
tokens; only their SHA-256 digest is stored, so a database leak does not expose
usable tokens.
"""

import hashlib
import hmac
import os
import secrets
import sqlite3
from base64 import urlsafe_b64decode, urlsafe_b64encode
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import Depends, Header, HTTPException

from backend.appdb.database import db

# scrypt parameters (interactive login): ~16 MiB, well under a second.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_DKLEN = 32

# Credential policy: the single source of truth for the API, the CLI, and the UI.
USERNAME_MIN_LENGTH = 3
USERNAME_MAX_LENGTH = 64
PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_LENGTH = 256


def _env_flag(name: str, default: bool) -> bool:
    """Parse a boolean environment flag, falling back to ``default`` when unset."""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


SESSION_TTL_HOURS = int(os.getenv("SESSION_TTL_HOURS", "168"))
# Registration defaults to closed; accounts are provisioned with
# `backend.appdb.manage`.
REGISTRATION_OPEN = _env_flag("AUTH_REGISTRATION_OPEN", default=False)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_DKLEN,
    )
    return "$".join(
        [
            "scrypt",
            str(_SCRYPT_N),
            str(_SCRYPT_R),
            str(_SCRYPT_P),
            urlsafe_b64encode(salt).decode(),
            urlsafe_b64encode(digest).decode(),
        ]
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_b64, digest_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        salt = urlsafe_b64decode(salt_b64)
        expected = urlsafe_b64decode(digest_b64)
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
        return hmac.compare_digest(digest, expected)
    except (ValueError, TypeError):
        return False


def create_user(username: str, password: str) -> dict:
    user_id = str(uuid4())
    now = _iso(_now())
    with db() as cur:
        try:
            cur.execute(
                "INSERT INTO users(id, username, password_hash, created_at) "
                "VALUES (?, ?, ?, ?)",
                (user_id, username, hash_password(password), now),
            )
        except sqlite3.IntegrityError as e:
            raise ValueError("That username is already taken.") from e
    return {"id": user_id, "username": username, "created_at": now}


def authenticate(username: str, password: str) -> dict | None:
    with db() as cur:
        row = cur.execute(
            "SELECT id, username, password_hash FROM users WHERE username = ?",
            (username,),
        ).fetchone()
    if row is None or not verify_password(password, row["password_hash"]):
        return None
    return {"id": row["id"], "username": row["username"]}


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(user_id: str) -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    now = _now()
    expires = now + timedelta(hours=SESSION_TTL_HOURS)
    with db() as cur:
        cur.execute(
            "INSERT INTO sessions(token_hash, user_id, created_at, expires_at) "
            "VALUES (?, ?, ?, ?)",
            (_hash_token(token), user_id, _iso(now), _iso(expires)),
        )
    return token, _iso(expires)


def _delete_session(token_hash: str) -> None:
    with db() as cur:
        cur.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))


def resolve_session(token: str) -> dict | None:
    """Resolve a bearer token to its user.

    An expired session is deleted opportunistically here (cheap, and keeps the
    table from accumulating dead rows); ``purge_expired_sessions`` sweeps the
    rest at startup. Expiry is compared in Python rather than SQL because
    ``expires_at`` is ISO text and a lexicographic comparison is not
    order-correct across the microsecond boundary.
    """
    token_hash = _hash_token(token)
    with db() as cur:
        row = cur.execute(
            "SELECT u.id, u.username, s.expires_at "
            "FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token_hash = ?",
            (token_hash,),
        ).fetchone()
    if row is None:
        return None
    if datetime.fromisoformat(row["expires_at"]) <= _now():
        _delete_session(token_hash)
        return None
    return {"id": row["id"], "username": row["username"]}


def revoke_session(token: str) -> None:
    _delete_session(_hash_token(token))


def purge_expired_sessions() -> None:
    with db() as cur:
        cur.execute("DELETE FROM sessions WHERE expires_at <= ?", (_iso(_now()),))


# --- FastAPI dependencies -----------------------------------------------------


def bearer_token(authorization: str | None = Header(default=None)) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=401,
            detail="Missing bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return authorization.split(" ", 1)[1].strip()


def require_user(token: str = Depends(bearer_token)) -> dict:
    user = resolve_session(token)
    if user is None:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired session. Please sign in again.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user
