"""MCP tool generation: every evidence tool is registered from the spec table.

A tool is a :class:`~backend.pipeline.tools.specs.ToolSpec`
(``pipeline/tools/specs.py``); this module turns each into a server tool whose
input schema is the spec's argument model (so bounds, enums and parameter
descriptions reach the client) and whose result is the shared envelope.
"""

from __future__ import annotations

from functools import partial
from typing import Any

import anyio
from fastmcp.exceptions import ToolError
from fastmcp.tools import Tool, ToolResult
from mcp.types import ToolAnnotations
from pydantic import Field, ValidationError

from backend.mcp_server.app import mcp
from backend.mcp_server.errors import to_tool_error
from backend.mcp_server.skills import list_skill_infos, read_skill_text
from backend.pipeline.tools.envelope import ToolEnvelope, run_enveloped
from backend.pipeline.tools.specs import ToolSpec, available_specs


def _strip_titles(node: Any, *, top: bool = False) -> None:
    """Drop the schema noise pydantic adds: per-field titles and, at the top, the
    model's docstring (the tool's own description is what clients should read)."""
    if not isinstance(node, dict):
        return
    node.pop("title", None)
    if top:
        node.pop("description", None)
    for sub in (node.get("properties") or {}).values():
        _strip_titles(sub)
    for key in ("items", "additionalItems"):
        _strip_titles(node.get(key))
    for key in ("anyOf", "oneOf", "allOf", "prefixItems"):
        for sub in node.get(key) or []:
            _strip_titles(sub)


def input_schema(spec: ToolSpec) -> dict[str, Any]:
    """The JSON schema a client sees for ``spec``'s arguments."""
    if spec.args_model is None:
        schema: dict[str, Any] = {"type": "object", "properties": {}}
    else:
        schema = spec.args_model.model_json_schema()
    _strip_titles(schema, top=True)
    schema["additionalProperties"] = False
    return schema


def validate_arguments(spec: ToolSpec, arguments: dict[str, Any]) -> dict[str, Any]:
    """The validated arguments of a call (defaults filled in), or a ``ToolError``
    that says what to fix. Unknown arguments are rejected rather than ignored, so a
    misspelt filter is not silently dropped."""
    allowed = set(spec.args_model.model_fields) if spec.args_model else set()
    unknown = sorted(set(arguments) - allowed)
    if unknown:
        raise ToolError(
            f"{spec.name} got unknown argument(s) {', '.join(unknown)}; "
            f"accepted: {', '.join(sorted(allowed)) or 'none'}."
        )
    if spec.args_model is None:
        return {}
    try:
        return spec.args_model.model_validate(arguments).model_dump()
    except ValidationError as error:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in err['loc']) or 'arguments'}: {err['msg']}"
            for err in error.errors(include_url=False, include_context=False)
        )
        raise ToolError(f"Invalid arguments for {spec.name}: {problems}") from None


def output_schema() -> dict[str, Any]:
    """The JSON schema of every evidence tool's result (the shared envelope)."""
    schema = ToolEnvelope.model_json_schema()
    _strip_titles(schema)
    return schema


def call_spec(spec: ToolSpec, arguments: dict[str, Any]) -> dict[str, Any]:
    """Validate and run one call, returning the result envelope (or a refusal).

    Bad arguments and real failures become a ``ToolError`` that says what to fix;
    a refused query is not a failure and comes back as a result.
    """
    args = validate_arguments(spec, arguments)
    try:
        return run_enveloped(
            function=lambda: spec.run(args, None), **spec.envelope_kwargs(args)
        )
    except Exception as error:
        raise to_tool_error(spec.name, error) from error


def annotations_for(spec: ToolSpec) -> ToolAnnotations:
    """Advisory hints clients use to decide when to ask before running a tool."""
    return ToolAnnotations(
        title=spec.title,
        readOnlyHint=spec.read_only,
        # Only meaningful for a tool that writes (the sweep builds, then drops, an
        # index on disk); it never deletes anything that was there.
        destructiveHint=None if spec.read_only else False,
        idempotentHint=True,
        openWorldHint=spec.open_world,
    )


class SpecTool(Tool):
    """A server tool backed by a :class:`ToolSpec`."""

    spec: Any = Field(exclude=True)

    @classmethod
    def from_spec(cls, spec: ToolSpec) -> SpecTool:
        return cls(
            name=spec.name,
            title=spec.title,
            description=spec.description,
            parameters=input_schema(spec),
            output_schema=output_schema(),
            annotations=annotations_for(spec),
            timeout=spec.timeout,
            spec=spec,
        )

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        # The query layer is blocking (psycopg2), so it runs on a worker thread.
        envelope = await anyio.to_thread.run_sync(
            partial(call_spec, self.spec, arguments)
        )
        return ToolResult(structured_content=envelope)


for _spec in available_specs():
    mcp.add_tool(SpecTool.from_spec(_spec))


@mcp.tool(
    name="list_skills",
    title="List investigation skills",
    annotations=ToolAnnotations(
        readOnlyHint=True, idempotentHint=True, openWorldHint=False
    ),
)
def list_skills() -> list[dict[str, str]]:
    """List the investigation skills (evidence plans per role, and the hypothesis
    workflow) with a one-line description of when each applies. Read one with
    read_skill, or as the resource skill://<name>/SKILL.md."""
    return list_skill_infos()


@mcp.tool(
    name="read_skill",
    title="Read an investigation skill",
    annotations=ToolAnnotations(
        readOnlyHint=True, idempotentHint=True, openWorldHint=False
    ),
)
def read_skill(name: str) -> str:
    """Read one investigation skill by name (see list_skills). A skill is the
    evidence plan to follow for a role, or the hypothesis workflow for a 'why'
    question. Names cannot contain paths."""
    return read_skill_text(name)
