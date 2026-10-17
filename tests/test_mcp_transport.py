"""Transports, authentication and the health probe (plan M4)."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time

import pytest
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StdioTransport
from starlette.testclient import TestClient

from backend.mcp_server import auth, http
from backend.mcp_server.auth import BearerTokenVerifier

TOKEN = "s3cret-token"
INITIALIZE = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}
MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


def _secured_app():
    """A server that behaves like ours with a token configured."""
    server = FastMCP("secured", auth=BearerTokenVerifier(TOKEN))
    server.custom_route("/health", methods=["GET"])(http.health)
    return server, http.create_http_app(server, host="127.0.0.1", path="/mcp")


# --- The token -----------------------------------------------------------------
def test_only_the_configured_token_is_accepted():
    verifier = BearerTokenVerifier(TOKEN)
    assert asyncio.run(verifier.verify_token(TOKEN)) is not None
    for wrong in ("", "wrong", TOKEN + "x", TOKEN[:-1], TOKEN.upper(), "tökén"):
        assert asyncio.run(verifier.verify_token(wrong)) is None


def test_an_empty_token_is_refused_at_construction():
    with pytest.raises(ValueError):
        BearerTokenVerifier("")


def test_the_comparison_is_constant_time(monkeypatch):
    seen = []
    real = auth.hmac.compare_digest
    monkeypatch.setattr(
        auth.hmac, "compare_digest", lambda a, b: seen.append((a, b)) or real(a, b)
    )
    asyncio.run(BearerTokenVerifier(TOKEN).verify_token("guess"))
    assert seen == [(b"guess", TOKEN.encode())]


def test_no_token_means_no_authentication_on_loopback(monkeypatch):
    monkeypatch.setattr(auth, "MCP_AUTH_TOKEN", None)
    assert auth.build_auth() is None
    monkeypatch.setattr(auth, "MCP_AUTH_TOKEN", TOKEN)
    assert isinstance(auth.build_auth(), BearerTokenVerifier)


# --- The HTTP app ----------------------------------------------------------------
def test_http_requires_the_token_and_the_right_host():
    _, app = _secured_app()
    with TestClient(app, base_url="http://127.0.0.1:8010") as client:
        bearer = {"Authorization": f"Bearer {TOKEN}"}
        assert client.post("/mcp", json=INITIALIZE, headers=MCP_HEADERS).status_code == 401
        wrong = {**MCP_HEADERS, "Authorization": "Bearer nope"}
        assert client.post("/mcp", json=INITIALIZE, headers=wrong).status_code == 401
        ok = client.post("/mcp", json=INITIALIZE, headers={**MCP_HEADERS, **bearer})
        assert ok.status_code == 200
        # A page that reached the server by DNS rebinding sends the attacker's host
        # (and its origin): refused even with a valid token.
        rebound = {**MCP_HEADERS, **bearer, "Host": "evil.example"}
        assert client.post("/mcp", json=INITIALIZE, headers=rebound).status_code == 421
        foreign = {**MCP_HEADERS, **bearer, "Origin": "https://evil.example"}
        assert client.post("/mcp", json=INITIALIZE, headers=foreign).status_code == 403


def test_the_health_route_needs_no_token_and_reports_the_database(monkeypatch):
    _, app = _secured_app()
    with TestClient(app, base_url="http://127.0.0.1:8010") as client:
        monkeypatch.setattr(http, "_database_answers", lambda: None)
        up = client.get("/health")
        assert (up.status_code, up.json()) == (200, {"status": "ok"})

        def down():
            raise RuntimeError("connection to db.internal:7890 refused, user=postgres")

        monkeypatch.setattr(http, "_database_answers", down)
        broken = client.get("/health")
        assert broken.status_code == 503
        assert broken.json() == {"status": "unavailable"}
        assert "db.internal" not in broken.text and "postgres" not in broken.text


def test_the_real_server_has_the_health_route_and_no_auth_without_a_token():
    from backend.mcp_server.server import mcp

    app = http.create_http_app(mcp, host="127.0.0.1", path="/mcp")
    with TestClient(app, base_url="http://127.0.0.1:8010") as client:
        assert client.get("/health").status_code in (200, 503)
        assert client.post("/mcp", json=INITIALIZE, headers=MCP_HEADERS).status_code == 200


def test_a_wildcard_bind_still_accepts_the_pipelines_own_client(monkeypatch):
    """The chatbot connects to MCP_HOST:MCP_PORT; for 0.0.0.0 that Host header
    names an address FastMCP does not allow by itself."""
    monkeypatch.setattr(http.fastmcp.settings, "http_allowed_hosts", None)
    assert http.allowed_hosts("127.0.0.1") is None
    assert http.allowed_hosts("0.0.0.0") == ["0.0.0.0"]
    monkeypatch.setattr(http.fastmcp.settings, "http_allowed_hosts", ["mcp.example.com"])
    assert http.allowed_hosts("0.0.0.0") == ["mcp.example.com", "0.0.0.0"]
    server = FastMCP("wild")
    app = http.create_http_app(server, host="0.0.0.0", path="/mcp")
    with TestClient(app, base_url="http://0.0.0.0:8010") as client:
        assert client.post("/mcp", json=INITIALIZE, headers=MCP_HEADERS).status_code == 200
    with TestClient(app, base_url="http://mcp.example.com") as client:
        assert client.post("/mcp", json=INITIALIZE, headers=MCP_HEADERS).status_code == 200
        client.headers["Host"] = "other.example"
        assert client.post("/mcp", json=INITIALIZE, headers=MCP_HEADERS).status_code == 421


# --- stdio ---------------------------------------------------------------------------
def test_the_server_loads_without_the_pipelines_provider_keys():
    """The MCP service must start for a judge who has none of the chatbot's model
    keys: it may not import anything that builds a model client at import."""
    code = (
        "import sys; import backend.mcp_server.server; "
        "bad = [m for m in ('backend.pipeline.models', 'backend.pipeline.decision', "
        "'backend.pipeline.graph', 'backend.api') if m in sys.modules]; "
        "print('LOADED' if not bad else bad)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120
    )
    assert result.stdout.strip().endswith("LOADED"), result.stderr[-500:]


def test_stdio_serves_the_same_server_and_keeps_stdout_clean():
    """Claude Desktop launches the process and speaks JSON-RPC over its pipes. A
    stray print would corrupt the stream, so a successful session proves stdout
    carries protocol messages only."""

    async def session():
        transport = StdioTransport(
            command=sys.executable,
            args=["-m", "backend.mcp_server", "--transport", "stdio"],
            env=dict(os.environ),
        )
        async with Client(transport) as client:
            tools = {t.name for t in await client.list_tools()}
            skills = await client.call_tool("list_skills", {})
            return tools, client.instructions, skills.is_error

    tools, instructions, failed = asyncio.run(asyncio.wait_for(session(), timeout=120))
    assert "search_logs_tool" in tools and "read_skill" in tools
    assert instructions and "list_datasets_tool" in instructions
    assert not failed


# --- A real HTTP server (gated: starts a process and binds a port) ---------------------
@pytest.mark.skipif(
    not os.getenv("RUN_MCP_SMOKE"), reason="set RUN_MCP_SMOKE=1 to start a real server"
)
def test_a_real_http_server_enforces_the_token_and_serves_tools():
    import socket

    import requests

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = {**os.environ, "MCP_HOST": "127.0.0.1", "MCP_PORT": str(port), "MCP_AUTH_TOKEN": TOKEN}
    server = subprocess.Popen(
        [sys.executable, "-m", "backend.mcp_server"], env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}/mcp"
    try:
        for _ in range(120):
            try:
                requests.get(f"http://127.0.0.1:{port}/health", timeout=1)
                break
            except requests.ConnectionError:
                time.sleep(0.5)
        assert requests.post(url, json=INITIALIZE, headers=MCP_HEADERS, timeout=5).status_code == 401
        rebound = {**MCP_HEADERS, "Authorization": f"Bearer {TOKEN}", "Host": "evil.example"}
        assert requests.post(url, json=INITIALIZE, headers=rebound, timeout=5).status_code == 421
        assert requests.get(f"http://127.0.0.1:{port}/health", timeout=5).status_code in (200, 503)

        async def session():
            async with Client(url, auth=TOKEN) as client:
                return {t.name for t in await client.list_tools()}

        assert "search_logs_tool" in asyncio.run(session())
    finally:
        server.terminate()
        server.wait(timeout=10)
