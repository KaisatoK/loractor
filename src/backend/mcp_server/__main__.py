"""Run the SereneDB MCP server over Streamable HTTP."""

import uvicorn
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from backend.mcp_server.config import MCP_AUTH_TOKEN, MCP_HOST, MCP_PATH, MCP_PORT
from backend.mcp_server.server import mcp


class BearerTokenMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, token: str):
        super().__init__(app)
        self.token = token

    async def dispatch(self, request: Request, call_next):
        if request.headers.get("authorization") != f"Bearer {self.token}":
            return PlainTextResponse("Unauthorized", status_code=401)
        return await call_next(request)


def main() -> None:
    app = mcp.http_app(path=MCP_PATH, transport="streamable-http")
    if MCP_AUTH_TOKEN:
        app.add_middleware(BearerTokenMiddleware, token=MCP_AUTH_TOKEN)
    uvicorn.run(app, host=MCP_HOST, port=MCP_PORT)


if __name__ == "__main__":
    main()
