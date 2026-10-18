# MCP server

The MCP (Model Context Protocol) server exposes the same search and summary tools the chatbot's own agent uses, plus the investigation skills, ready-made role prompts and the rules the answers are held to. An external agent (Claude Code, Claude Desktop, Kiro, MCP Inspector) can investigate the same logs the chatbot does, with the same contract.

## What the server offers

**Tools.** Sixteen evidence tools, plus `list_skills` and `read_skill`. Fourteen are the pipeline's own; two are server-only.

- `list_datasets_tool`, `count_by_dataset_tool`, `get_trend_tool`, `count_by_category_tool`, `sweep_chunks_tool`, `investigate_chunk_tool`, `search_logs_tool`, `otel_index_tool`, `top_in_category_by_service_tool`, `compare_periods_tool`, `get_data_tool`, `get_log_by_line_id_tool`, `get_nearby_events_tool`, `count_by_team_tool` — the pipeline's own fourteen.
- `rank_priorities_tool` — **server-only**: an external agent has no answer stage, so it needs the ranking computed for it. The chatbot adds its own ranking record in `answer_node` instead.
- `get_trace_logs_tool` — **server-only**: it follows one OTel trace id across services, which the chatbot's graph does not currently ask for.

Each tool is declared once in `src/backend/pipeline/tools/specs.py` and generated for the server, so its name, description, arguments and bounds match the pipeline's. The full catalogue is in [TOOLS.md](../TOOLS.md).

**Skills.** `list_skills` and `read_skill` serve the same playbooks the pipeline injects: `engineer-investigation`, `recurring-patterns`, `risk-prioritization` and the shared `log-investigation` hypothesis workflow.

**Prompts.** `investigate_as_engineer`, `investigate_as_team_lead`, `investigate_as_manager` (arguments `question`, `effort`) and `worked_investigation` (arguments `topic`, `effort`). Each carries the same briefing the chatbot's evidence agent receives — role objective, evidence plan, team ownership, effort budget — plus the answer rules and layout.

**Resources.** The skills (`skill://<name>/SKILL.md`, `skill://<name>/_manifest`), `logs://datasets/{dataset_id}/schema`, `logs://ownership` (fictional), `logs://roles/{role}`, and `logs://policy/{result-contract,priority-score,effort}`.

**Health.** `GET /health` (HTTP, no token) returns 200 when the log database answers and 503 otherwise, with no detail.

## How it is built

The server is assembled from the same sources as the pipeline and holds no text of its own: tools come from the spec table, guidance from the role and envelope modules, prompts from the same prompt builders the pipeline uses, and resources from the dataset registry, the role and effort tables, the priority formula and the ownership file. It loads without any of the chatbot's model keys, so it can run as a standalone service.

## Run it

```bash
bash ./mcp_server.sh                                   # Streamable HTTP at 127.0.0.1:8010/mcp
uv run python -m backend.mcp_server --transport stdio  # a process the client launches
TOOLS_TRANSPORT=mcp bash ./backend.sh                   # the chatbot's own pipeline uses the server
```

The backend defaults to in-process tools; setting `TOOLS_TRANSPORT=mcp` routes the evidence tools through the server, and the backend falls back to the in-process implementation if the service is unreachable. Auth is a bearer token via `MCP_AUTH_TOKEN`: loopback needs no token, and a non-loopback bind requires one (the server refuses an unauthenticated non-loopback configuration). `/auto-complete` is a chat-box feature and is always answered in-process, not over MCP.

The server's own configuration (`MCP_HOST`, `MCP_PORT`, `MCP_PATH`, `MCP_AUTH_TOKEN`, the call timeouts, the sweep limits, rate limiting and caching) is documented in the README's [MCP tool service](../README.md#mcp-tool-service) section.

## Connect Claude

Start the server, then point the client at it:

```text
Endpoint:    http://127.0.0.1:8010/mcp
Transport:   Streamable HTTP
Authorization: Bearer <MCP_AUTH_TOKEN>      # only when MCP_AUTH_TOKEN is set
```

For a stdio client, `fastmcp.json` at the repo root describes the launch, so `fastmcp install claude-code fastmcp.json` (or `claude-desktop`) can register it. After connecting, the agent receives the server's instructions; pick a role prompt such as `investigate_as_manager` to start the same workflow the chatbot runs. Keep the actual token out of committed files. The full configuration, including the HTTP and stdio JSON, is in the README's [MCP tool service](../README.md#mcp-tool-service) and [Connecting an external AI agent](../README.md#connecting-an-external-ai-agent) sections.
