"""Run the SereneDB MCP server.

    python -m backend.mcp_server                    Streamable HTTP (default)
    python -m backend.mcp_server --transport stdio  a process the client launches

HTTP listens on ``MCP_HOST:MCP_PORT``; an agent on the same machine, or the
chatbot's own pipeline (``TOOLS_TRANSPORT=mcp``), connects to it. stdio is for
clients that start the server themselves (Claude Desktop, ``fastmcp install``):
nothing but protocol messages may reach stdout, so logging goes to stderr.
"""

from __future__ import annotations

import argparse

from backend.mcp_server.config import MCP_HOST, MCP_PORT


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m backend.mcp_server")
    parser.add_argument(
        "--transport",
        choices=("http", "stdio"),
        default="http",
        help="http (default) listens on MCP_HOST:MCP_PORT; stdio talks over the "
        "process's stdin/stdout",
    )
    args = parser.parse_args(argv)

    # Imported here so --help and argument errors do not load the whole server.
    from backend.mcp_server.server import mcp

    if args.transport == "stdio":
        mcp.run(transport="stdio", show_banner=False)
        return

    import uvicorn

    from backend.mcp_server.http import create_http_app

    uvicorn.run(create_http_app(), host=MCP_HOST, port=MCP_PORT)


if __name__ == "__main__":
    main()
