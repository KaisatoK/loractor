import jsonref

from pathlib import Path

LOG_SCHEMA_PATH = Path(__file__).resolve().parents[1] / "db" / "log_table_schema.yaml"
LOG_SCHEMA = LOG_SCHEMA_PATH.read_text(encoding="utf-8")
TOOL_SCHEMA_PATH = Path(__file__).resolve().parents[0] / "tool_schema.json"
TOOL_SCHEMA = jsonref.loads(TOOL_SCHEMA_PATH.read_text(encoding="utf-8"))


def load_tool_schema(name: str):
    tools: list[dict] = TOOL_SCHEMA["tool_catalog"]["tools"]

    for tool in tools:
        if tool["name"] == name:
            return tool
