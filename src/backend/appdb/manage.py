"""Create a user from the command line.
Usage: ``uv run python -m backend.appdb.manage <username> <password>``.
Bootstraps the first account while ``AUTH_REGISTRATION_OPEN`` is false."""

import sys

from backend.appdb.auth import (
    PASSWORD_MAX_LENGTH,
    PASSWORD_MIN_LENGTH,
    USERNAME_MAX_LENGTH,
    USERNAME_MIN_LENGTH,
    create_user,
)


def provision(username: str, password: str) -> tuple[int, str]:
    """Create a user under the shared credential policy.
    Returns ``(exit_code, message)``: 0 created, 1 taken, 2 policy violation.
    """
    if len(username) < USERNAME_MIN_LENGTH:
        return 2, f"Username must be at least {USERNAME_MIN_LENGTH} characters."
    if len(username) > USERNAME_MAX_LENGTH:
        return 2, f"Username must be at most {USERNAME_MAX_LENGTH} characters."
    if len(password) < PASSWORD_MIN_LENGTH:
        return 2, f"Password must be at least {PASSWORD_MIN_LENGTH} characters."
    if len(password) > PASSWORD_MAX_LENGTH:
        return 2, f"Password must be at most {PASSWORD_MAX_LENGTH} characters."
    try:
        user = create_user(username, password)
    except ValueError as err:
        return 1, f"Error: {err}"
    return 0, f"Created user {user['username']!r} (id={user['id']})."


def main() -> int:
    if len(sys.argv) != 3:
        print("Usage: python -m backend.appdb.manage <username> <password>")
        return 2
    username, password = sys.argv[1], sys.argv[2]
    code, message = provision(username, password)
    print(message)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
