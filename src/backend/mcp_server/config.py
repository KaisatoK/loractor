"""Configuration for the standalone Streamable HTTP MCP service."""

from __future__ import annotations

import ipaddress
import os
from urllib.parse import urljoin


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


MCP_HOST = os.getenv("MCP_HOST", "127.0.0.1")
MCP_PORT = _env_int("MCP_PORT", 8010)
MCP_PATH = os.getenv("MCP_PATH", "/mcp")
if not MCP_PATH.startswith("/"):
    MCP_PATH = "/" + MCP_PATH
MCP_AUTH_TOKEN = os.getenv("MCP_AUTH_TOKEN", "").strip() or None
MCP_CALL_TIMEOUT_S = float(os.getenv("MCP_CALL_TIMEOUT_S", "60"))
MCP_SKILLS_ROOT = os.getenv("MCP_SKILLS_ROOT", "") or None
TOOLS_TRANSPORT = os.getenv("TOOLS_TRANSPORT", "local").lower()
if TOOLS_TRANSPORT not in {"local", "mcp"}:
    raise ValueError("TOOLS_TRANSPORT must be 'local' or 'mcp'")


def is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


if not is_loopback(MCP_HOST) and not MCP_AUTH_TOKEN:
    raise RuntimeError("MCP_AUTH_TOKEN is required when MCP_HOST is not loopback")

MCP_URL = urljoin(f"http://{MCP_HOST}:{MCP_PORT}", MCP_PATH.lstrip("/"))
