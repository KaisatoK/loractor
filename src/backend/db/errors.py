"""Query refusals and the exception that carries them.

A query that cannot be run safely is **refused**, not attempted. The refusal
travels to the agent as a tool result it can reason about -- "the window is too
wide, try 60 seconds" -- rather than as an exception that tears the stream or a
30-second hang that ends in a statement timeout.

That distinction is the whole point: the agent's next move after a refusal is to
narrow and retry, which it can only do if it is told what was wrong and what
would work instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Refusal:
    """A structured explanation of why a query was not run."""

    reason: str
    """What was wrong, in terms the agent can act on."""

    suggestion: str
    """A concrete narrower request that would be accepted."""

    code: str = "refused"
    """Stable machine-readable category, for tests and metrics."""

    details: dict = field(default_factory=dict)
    """Supporting numbers (estimated rows, allowance, window seconds)."""

    def as_tool_result(self) -> dict:
        """The payload the tool layer hands back to the agent."""
        return {
            "status": "refused",
            "code": self.code,
            "reason": self.reason,
            "suggestion": self.suggestion,
            **self.details,
        }


class QueryRefused(Exception):
    """Raised by a query builder when the request cannot be run safely.

    Caught at the tool boundary and converted into a refusal result, so the
    model never sees a traceback and the stream is never torn.
    """

    def __init__(self, refusal: Refusal) -> None:
        super().__init__(refusal.reason)
        self.refusal = refusal


def refuse(
    reason: str,
    suggestion: str,
    *,
    code: str = "refused",
    **details: object,
) -> QueryRefused:
    """Build a :class:`QueryRefused` in one call."""
    return QueryRefused(
        Refusal(reason=reason, suggestion=suggestion, code=code, details=dict(details))
    )
