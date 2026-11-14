"""Anthropic Messages API provider (official ``anthropic`` SDK, manual tool loop).

Reads the key from ``ANTHROPIC_API_KEY`` (or the variable named in
``api_key_env``). The full assistant ``content`` is appended back to the
conversation unchanged on every turn, so thinking blocks are preserved.

Deliberately NOT used: server-side refusal ``fallbacks``. A fallback would
silently answer with a different model, which would contaminate the
experiment. A refusal is recorded as the run's outcome instead.
Sampling parameters (temperature etc.) are not sent: current Claude models
reject them, and the provider default is used and recorded.
"""

from __future__ import annotations

import os

from .base import ModelError, ToolCall, ToolResult, Turn


class AnthropicModel:
    provider = "anthropic"

    def __init__(
        self,
        label: str,
        model: str,
        max_tokens: int = 16000,
        effort: str | None = None,
        api_key_env: str = "ANTHROPIC_API_KEY",
        max_retries: int = 4,
        timeout: float = 600.0,
        **_: object,
    ) -> None:
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - depends on install
            raise ModelError("pip install 'agent-trust-testbed[anthropic]' to use the anthropic provider") from exc
        key = os.environ.get(api_key_env)
        if not key:
            raise ModelError(f"environment variable {api_key_env} is not set")
        self._anthropic = anthropic
        self.client = anthropic.Anthropic(api_key=key, max_retries=max_retries, timeout=timeout)
        self.label = label
        self.model_id = model
        self.max_tokens = max_tokens
        self.effort = effort

    def describe(self) -> dict:
        return {
            "label": self.label,
            "provider": self.provider,
            "model": self.model_id,
            "max_tokens": self.max_tokens,
            "effort": self.effort,
            "sdk_version": self._anthropic.__version__,
        }

    def start(self, system: str, user: str, tools: list[dict]) -> "AnthropicSession":
        return AnthropicSession(self, system, user, tools)


class AnthropicSession:
    def __init__(self, model: AnthropicModel, system: str, user: str, tools: list[dict]) -> None:
        self.model = model
        self.system = system
        self.tools = [
            {"name": t["name"], "description": t["description"], "input_schema": t["parameters"]} for t in tools
        ]
        self.messages: list[dict] = [{"role": "user", "content": user}]

    def step(self) -> Turn:
        m = self.model
        kwargs: dict = {
            "model": m.model_id,
            "max_tokens": m.max_tokens,
            "system": self.system,
            "tools": self.tools,
            "messages": self.messages,
        }
        if m.effort:
            kwargs["output_config"] = {"effort": m.effort}
        try:
            resp = m.client.messages.create(**kwargs)
        except m._anthropic.APIError as exc:
            raise ModelError(f"{exc.__class__.__name__}: {exc}") from exc

        self.messages.append({"role": "assistant", "content": resp.content})
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                args = block.input if isinstance(block.input, dict) else None
                calls.append(
                    ToolCall(block.id, block.name, args, parse_error=None if args is not None else "input is not an object")
                )
        extra = {}
        stop_details = getattr(resp, "stop_details", None)
        if stop_details is not None:
            extra["stop_details"] = stop_details.model_dump(mode="json") if hasattr(stop_details, "model_dump") else str(stop_details)
        return Turn(
            text="\n".join(text_parts),
            tool_calls=calls,
            stop_reason=str(resp.stop_reason),
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
            model_version=resp.model,
            raw=resp.model_dump(mode="json"),
            extra=extra,
        )

    def add_tool_results(self, results: list[ToolResult]) -> None:
        # All results for one assistant turn go back in a single user message.
        self.messages.append(
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": r.call_id, "content": r.content, "is_error": r.is_error}
                    for r in results
                ],
            }
        )
