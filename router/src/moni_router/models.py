"""Typed model-facing values for the router.

Conversation messages are LangChain message objects (``SystemMessage``, ``HumanMessage``,
``AIMessage``, ``ToolMessage``), not a bespoke type: the graph's state uses them because
LangGraph's ``add_messages`` reducer normalises to them, and one representation across the
codebase beats two that must be kept in step. They are converted to OpenAI's wire shape
inside :mod:`moni_router.chat`, at the HTTP boundary.

The graph never handles raw dicts: every model call is expressed with these types, so a
malformed response is a validation error rather than a silent ``KeyError`` deep in a
node.

``ToolSpec`` deliberately has **no** ``user_context`` parameter. Identity is injected by
the agent when it *executes* a tool, never chosen by the model (CLAUDE.md §3.2); the
schemas handed to the model are built from the MCP server's own list, minus the identity
argument.
"""

from __future__ import annotations

from typing import Any, Final, Literal

from pydantic import BaseModel, Field

Role = Literal["system", "user", "assistant", "tool"]

# The three data levels of CLAUDE.md §3.4. `A` is the most restrictive and the fail-closed
# default: "unknown data level → treat as A" (§3.12).
#
# The order matters and it is *not* alphabetical-by-permissiveness: A > B > C, so composing two
# levels is a maximum over this ordering (see moni_router.classifier.LEVEL_ORDER). Getting the
# ordering backwards would be invisible in a unit test that only ever composes one level with
# itself, which is why the classifier asserts it explicitly.
DataLevel = Literal["A", "B", "C"]
PHASE1_LEVEL: Final[DataLevel] = "A"

#: Where a model call actually went. Reported per call, so "did this leave the server?" is
#: answerable for a step rather than for a run (§3.4, §3.8).
Destination = Literal["local", "cloud"]


class ToolParameter(BaseModel):
    """One parameter of a tool, as advertised to the model."""

    name: str
    type: str = "string"
    description: str | None = None
    required: bool = True


class ToolSpec(BaseModel):
    """A callable tool the model may choose.

    ``parameters`` never includes ``user_context``: the agent adds it server-side.
    """

    name: str
    description: str
    parameters: list[ToolParameter] = Field(default_factory=list)

    def to_openai_schema(self) -> dict[str, Any]:
        """The ``tools=[...]`` entry for an OpenAI-compatible endpoint."""
        properties: dict[str, Any] = {}
        for parameter in self.parameters:
            schema: dict[str, Any] = {"type": parameter.type}
            if parameter.description:
                schema["description"] = parameter.description
            properties[parameter.name] = schema
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": [p.name for p in self.parameters if p.required],
                },
            },
        }


class ToolCall(BaseModel):
    """A tool invocation requested by the model."""

    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)

    def argument_json(self) -> str:
        import json

        return json.dumps(self.arguments, sort_keys=True, default=str)


class ChatResult(BaseModel):
    """The outcome of one model call."""

    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    finish_reason: str | None = None
    model: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None

    # -- routing facts (task 2.4, §3.4/§3.8) ---------------------------------
    #
    # The router owns the *decision* (which destination, at which level, anonymised or not) and
    # the agent owns the tracer and the audit row. These three fields are how the decision gets
    # from one to the other without the agent re-deriving it — a re-derivation would be a second
    # implementation of the policy, and the two would eventually disagree.
    #
    # All of them are defaulted, which is deliberate: every construction of a ChatResult that
    # predates this task keeps working, and the defaults are the fail-closed reading of "the
    # router did not say" — the most private level, the on-server destination, no placeholders.
    level: DataLevel = PHASE1_LEVEL
    destination: Destination = "local"
    anonymized: bool = False
    #: True when a cloud failure sent this call to the local model instead of the cloud. Reported
    #: per call because "the run was degraded" is not the same statement as "this step was".
    degraded: bool = False
    #: Placeholders the model invented (``{CLIENT_9}``) and which were therefore left untouched.
    #: Counted, never resolved: inventing a mapping is a fabrication. A non-zero count is a
    #: signal about the model, not about the data, so it is reported rather than hidden.
    invented_placeholders: int = 0

    @property
    def wants_tool(self) -> bool:
        return bool(self.tool_calls)

    @property
    def is_empty(self) -> bool:
        """True when the call produced nothing usable: no text and no tool call.

        This is the "failed or empty local step" the escalation rule counts (§2). It is a
        property rather than a field because it *is* the definition of empty, and a field could
        be set inconsistently with the content it describes.
        """
        return not (self.content or "").strip() and not self.tool_calls


class StreamChunk(BaseModel):
    """One streamed delta. ``content`` is text; ``tool_call`` is a completed call."""

    content: str | None = None
    tool_call: ToolCall | None = None
    done: bool = False


__all__ = [
    "PHASE1_LEVEL",
    "ChatResult",
    "DataLevel",
    "Destination",
    "Role",
    "StreamChunk",
    "ToolCall",
    "ToolParameter",
    "ToolSpec",
]
