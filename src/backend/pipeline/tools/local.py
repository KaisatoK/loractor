"""The local transport: in-process LangChain tools, generated from the spec table.

Nothing here is written per tool. Each tool is a :class:`~backend.pipeline.tools.specs.ToolSpec`;
this module wraps its ``run`` in the executor (progress events, the result
envelope, refusals) and hands LangChain the spec's schema and description.
"""

from __future__ import annotations

from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool, StructuredTool

from backend.pipeline.tools.executor import execute_database_tool
from backend.pipeline.tools.specs import ToolSpec, available_specs


def _execute(spec: ToolSpec, runtime: ToolRuntime, args: dict) -> dict:
    """Run one call of ``spec`` the way every local tool runs."""
    if spec.reports_progress:
        # The sweep is the one tool that reports from inside itself. Its call can
        # run for minutes over 1,624 chunks, and a silent minute is
        # indistinguishable from a hang.
        function = lambda reporter: spec.run(args, reporter)  # noqa: E731
    else:
        function = lambda: spec.run(args, None)  # noqa: E731
    return execute_database_tool(
        runtime=runtime,
        function=function,
        with_reporter=spec.reports_progress,
        **spec.envelope_kwargs(args),
    )


def make_local_tool(spec: ToolSpec) -> BaseTool:
    """The LangChain tool for ``spec``.

    ``runtime`` is injected by the agent's ToolNode and stays out of the schema the
    model sees. A tool without model arguments passes no ``args_schema`` on
    purpose: an empty one makes langchain-core call it with no arguments at all,
    dropping the injected runtime (see ``test_no_tool_has_an_empty_args_schema``);
    inferred from the signature, the schema keeps ``runtime`` injected and shows
    the model no fields.
    """
    if spec.args_model is None:

        def func(runtime: ToolRuntime) -> dict:
            return _execute(spec, runtime, {})

    else:

        def func(runtime: ToolRuntime, **kwargs) -> dict:
            # ``kwargs`` are already validated against ``args_model`` by
            # langchain-core, defaults included.
            return _execute(spec, runtime, kwargs)

    func.__name__ = spec.name
    return StructuredTool.from_function(
        func=func,
        name=spec.name,
        description=spec.description,
        args_schema=spec.args_model,
    )


LOCAL_TOOLS: dict[str, BaseTool] = {
    spec.name: make_local_tool(spec) for spec in available_specs()
}
