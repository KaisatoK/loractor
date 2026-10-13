# Login

## What lives here

- `login.py` — the real sign-in / create-account page (`login_page()`) and
  `sign_in()`, which stores the authenticated session.
- `README.md` — this file.

## How real authentication works

Accounts, sessions, and chats are owned by the backend (`src/backend/appdb`); the
UI holds a bearer token. The auth endpoints are:

| Endpoint | Body / result |
| --- | --- |
| `POST /auth/register` | `{username, password}` → `{token, expires_at, user}` |
| `POST /auth/login` | `{username, password}` → `{token, expires_at, user}` |
| `POST /auth/logout` | revokes the bearer token → `{ok: true}` |
| `GET /auth/me` | current user → `{id, username}` |

On success `sign_in()` stores `{token, user, expires_at}` in
`st.session_state.auth`, and every request sends `Authorization: Bearer <token>`.
The backend gates every `/chat/*` and `/chats*` route with `require_user`.

Details live in `src/backend/appdb/auth.py`: passwords are hashed with scrypt
(stdlib only), only a SHA-256 digest of each session token is stored, and session
lifetime is `SESSION_TTL_HOURS` (default `168`, i.e. one week).

## Sign-in behaviour

| Action | Result |
| --- | --- |
| Backend unreachable | sign-in page plus a "Could not reach the backend" message (no traceback) |

## Gotchas

- `./backend.sh` is not executable on this checkout (`-rw-r--r--`); run
  `bash ./backend.sh` even though `AGENTS.md` documents `./backend.sh`.

## Demo auto-login (temporary)

### Why it exists

The demo exists so the hackathon demo skips typing credentials on every server
restart. It is temporary.

### Files, flag, and credentials

It lives in exactly two files:

- `src/ui/login/demo.py` — reads the flag and credentials and signs in.
- `src/backend/appdb/demo.py` — provisions the account on backend start.

It is controlled by `AUTH_DEMO_AUTOLOGIN` (**default ON**). Both sides must agree
on the credentials, `AUTH_DEMO_USER` / `AUTH_DEMO_PASSWORD` (defaults `abc` /
`12345678`). Setting `AUTH_DEMO_AUTOLOGIN=false` skips the auto sign-in and shows
the real sign-in page.

### Demo behaviour

| Action | Result |
| --- | --- |
| Fresh session / server restart | auto-signs in as the demo account |
| Click **Log out** | stays on the sign-in page for the rest of the session |
| `AUTH_DEMO_AUTOLOGIN=false` | starts on the sign-in page |

### How to remove the demo

1. Delete `src/ui/login/demo.py` and `src/backend/appdb/demo.py`.
2. Delete `tests/test_demo.py`.
3. Delete the demo provisioning comment and the
   `uv run python -m backend.appdb.demo` line from `backend.sh`.
4. In `src/ui/streamlit_app.py`: delete the `from ui.login import demo` import,
   delete the demo comment lines above the gate, delete the
   `demo.mark_logged_out()` line from `_logout()`, and reduce the gate to
   `login_page(); st.stop()`.
5. Delete the "Demo auto-login (temporary)" section from `src/ui/login/README.md`
   (the file itself stays — it also documents the permanent auth flow).
6. Delete the "Demo auto-login" section from `README.md`.

Once the demo is gone, `login_page()` is the only way in, so you need either
`AUTH_REGISTRATION_OPEN=true` (registration is closed by default) or an account
provisioned ahead of time with:

```bash
uv run python -m backend.appdb.manage <username> <password>
```

### How to change the demo

- **Credentials** — set `AUTH_DEMO_USER` and `AUTH_DEMO_PASSWORD` for *both* the
  UI and the backend, or edit the defaults in both `src/ui/login/demo.py` and
  `src/backend/appdb/demo.py`.
- **Start in real-auth mode** — set `AUTH_DEMO_AUTOLOGIN=false`.
- **Session lifetime** — set `SESSION_TTL_HOURS` (default `168`).

### Demo gotchas

- Removing the demo code does not remove the demo account: the row persists in
  `.data/app.sqlite`. Delete it there if you want it gone.
- `AUTH_DEMO_AUTOLOGIN=false` skips the auto sign-in but `./backend.sh` still
  provisions the account.
