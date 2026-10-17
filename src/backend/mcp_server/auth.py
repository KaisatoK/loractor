"""Bearer-token authentication for the HTTP transport.

The token comes from ``MCP_AUTH_TOKEN`` (``mcp_server/config.py`` refuses to bind a
non-loopback address without one). It is compared in constant time: a plain ``==``
returns as soon as a byte differs, which leaks how much of a guess was right.
Authentication applies to the HTTP transport only; over stdio the client launches
the process itself.
"""

from __future__ import annotations

import hmac

from fastmcp.server.auth import AccessToken, TokenVerifier

from backend.mcp_server.config import MCP_AUTH_TOKEN


class BearerTokenVerifier(TokenVerifier):
    """Accepts exactly one pre-shared bearer token."""

    def __init__(self, token: str) -> None:
        super().__init__()
        if not token:
            raise ValueError("an empty token would authenticate everyone")
        self._token = token.encode("utf-8")

    async def verify_token(self, token: str) -> AccessToken | None:
        # Bytes, so a non-ASCII guess is compared rather than raising.
        if not hmac.compare_digest(token.encode("utf-8"), self._token):
            return None
        return AccessToken(token=token, client_id="mcp-client", scopes=[])


def build_auth() -> BearerTokenVerifier | None:
    """The verifier for the configured token, or ``None`` when none is set."""
    return BearerTokenVerifier(MCP_AUTH_TOKEN) if MCP_AUTH_TOKEN else None
