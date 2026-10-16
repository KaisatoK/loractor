"""TEMPORARY: provisions the demo account the UI auto-logs in as.
Delete together with ``src/ui/login/demo.py`` (see its docstring). Idempotent."""

import os

from backend.appdb.manage import provision

DEFAULT_USERNAME = "abc"
DEFAULT_PASSWORD = "12345678"


def main() -> int:
    username = os.getenv("AUTH_DEMO_USER", DEFAULT_USERNAME)
    password = os.getenv("AUTH_DEMO_PASSWORD", DEFAULT_PASSWORD)
    code, message = provision(username, password)
    print(message)
    # A duplicate username is the expected steady state, not a failure.
    return 0 if code == 1 else code


if __name__ == "__main__":
    raise SystemExit(main())
