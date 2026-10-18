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
# The sweep tools run for minutes (four promotions is about three quarters of an
# hour), so the pipeline's bridge waits this long for them instead of
# MCP_CALL_TIMEOUT_S. Every other tool keeps the short timeout.
MCP_SWEEP_TIMEOUT_S = float(os.getenv("MCP_SWEEP_TIMEOUT_S", "3600"))
if MCP_CALL_TIMEOUT_S <= 0 or MCP_SWEEP_TIMEOUT_S <= 0:
    raise ValueError("MCP_CALL_TIMEOUT_S and MCP_SWEEP_TIMEOUT_S must be positive")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def _env_non_negative_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < 0:
        raise ValueError(f"{name} must not be negative")
    return value


# Whether external agents see the sweep tools (tag "sweep") at all.
MCP_ENABLE_SWEEP = _env_bool("MCP_ENABLE_SWEEP", True)
# Most chunks one call may read message text for (each costs about eleven minutes).
# The argument's own maximum is 4 (Brutal), so the default changes nothing; 0 allows
# the sweep's free passes but no promotion at all.
MCP_MAX_PROMOTIONS = _env_non_negative_int("MCP_MAX_PROMOTIONS", 4)
# Requests per second one client (bearer token, or "anonymous" without auth) may
# make; bursts up to twice that. 0 turns rate limiting off.
MCP_RATE_LIMIT_RPS = float(os.getenv("MCP_RATE_LIMIT_RPS", "50"))
# Seconds a cached count_by_dataset_tool result is reused; 0 turns caching off.
MCP_CACHE_TTL_S = _env_non_negative_int("MCP_CACHE_TTL_S", 300)
if MCP_RATE_LIMIT_RPS < 0:
    raise ValueError("MCP_RATE_LIMIT_RPS must not be negative")
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
