"""Create a user from the command line.

Usage:
    uv run python -m backend.appdb.manage <username> <password>

Useful for bootstrapping the first account while ``AUTH_REGISTRATION_OPEN`` is
false (or to provision accounts directly).
"""

import sys

from backend.appdb.auth import create_user


def main() -> int:
    if len(sys.argv) != 3:
        print("Usage: python -m backend.appdb.manage <username> <password>")
        return 2
    username, password = sys.argv[1], sys.argv[2]
    if len(password) < 8:
        print("Password must be at least 8 characters.")
        return 2
    try:
        user = create_user(username, password)
    except ValueError as err:
        print(f"Error: {err}")
        return 1
    print(f"Created user {user['username']!r} (id={user['id']}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
