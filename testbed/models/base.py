"""Provider-neutral model interface.

Each provider keeps its own native message list inside a session, so
provider-specific content (for example thinking blocks) is passed back
unchanged. The coordinator loop only sees ``Turn`` objects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict | None
    raw_arguments: str | None = None
    parse_error: str | None = None


@dataclass
class ToolResult:
    call_id: str
    name: str
    content: str
    is_error: bool = False


@dataclass
class Turn:
    text: str
    tool_calls: list[ToolCall]
    stop_reason: str
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0  # part of input_tokens served from a cache, when reported
    reasoning_tokens: int = 0  # part of output_tokens spent on reasoning, when reported
    reported_cost: float | None = None  # cost the provider itself reported (OpenRouter), in USD
    model_version: str = ""
    raw: Any = None
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "tool_calls": [
                {"id": c.id, "name": c.name, "arguments": c.arguments, "raw_arguments": c.raw_arguments, "parse_error": c.parse_error}
                for c in self.tool_calls
            ],
            "stop_reason": self.stop_reason,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "reported_cost": self.reported_cost,
            "model_version": self.model_version,
            "extra": self.extra,
            "raw": self.raw,
        }


class Session(Protocol):
    def step(self) -> Turn: ...

    def add_tool_results(self, results: list[ToolResult]) -> None: ...


class Model(Protocol):
    label: str
    provider: str
    model_id: str

    def start(self, system: str, user: str, tools: list[dict]) -> Session: ...

    def describe(self) -> dict: ...


class ModelError(RuntimeError):
    """The provider call failed (after the SDK's own retries)."""
