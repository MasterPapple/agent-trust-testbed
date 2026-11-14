"""OpenAI Chat Completions provider (official ``openai`` SDK).

Also covers any OpenAI-compatible endpoint via ``base_url``: Google Gemini's
OpenAI-compatible endpoint, or an open-weights model served locally with vLLM
or Ollama. Reads the key from ``OPENAI_API_KEY`` or the variable named in
``api_key_env``.
"""

from __future__ import annotations

import json
import os
import threading
import time

from .base import ModelError, ToolCall, ToolResult, Turn


class OpenAIModel:
    provider = "openai"

    def __init__(
        self,
        label: str,
        model: str,
        max_tokens: int | None = 16000,
        temperature: float | None = None,
        reasoning_effort: str | None = None,
        max_tokens_param: str = "max_completion_tokens",
        base_url: str | None = None,
        api_key_env: str = "OPENAI_API_KEY",
        max_retries: int = 4,
        timeout: float = 600.0,
        extra_body: dict | None = None,
        min_interval_s: float = 0.0,
        **_: object,
    ) -> None:
        try:
            import openai
        except ImportError as exc:  # pragma: no cover - depends on install
            raise ModelError("pip install 'agent-trust-testbed[openai]' to use the openai provider") from exc
        key = os.environ.get(api_key_env)
        if not key:
            raise ModelError(f"environment variable {api_key_env} is not set")
        self._openai = openai
        self.client = openai.OpenAI(api_key=key, base_url=base_url, max_retries=max_retries, timeout=timeout)
        self.label = label
        self.model_id = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.reasoning_effort = reasoning_effort
        self.base_url = base_url
        # Some OpenAI-compatible servers only accept the older "max_tokens" name.
        self.max_tokens_param = max_tokens_param
        # Extra JSON fields sent with every request (provider-specific options).
        self.extra_body = dict(extra_body or {})
        # Minimum seconds between requests for this model label, across all runs and
        # worker threads. Useful for free tiers with strict rate limits.
        self.min_interval_s = float(min_interval_s or 0.0)

    def describe(self) -> dict:
        return {
            "label": self.label,
            "provider": self.provider,
            "model": self.model_id,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "reasoning_effort": self.reasoning_effort,
            "base_url": self.base_url,
            "max_tokens_param": self.max_tokens_param,
            "extra_body": self.extra_body,
            "min_interval_s": self.min_interval_s,
            "sdk_version": self._openai.__version__,
        }

    def start(self, system: str, user: str, tools: list[dict]) -> "OpenAISession":
        return OpenAISession(self, system, user, tools)


class OpenAISession:
    def __init__(self, model: OpenAIModel, system: str, user: str, tools: list[dict]) -> None:
        self.model = model
        self.tools = [
            {"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["parameters"]}}
            for t in tools
        ]
        self.messages: list[dict] = [{"role": "system", "content": system}, {"role": "user", "content": user}]

    def step(self) -> Turn:
        m = self.model
        kwargs: dict = {"model": m.model_id, "messages": self.messages, "tools": self.tools}
        if m.max_tokens:
            kwargs[m.max_tokens_param] = m.max_tokens
        if m.temperature is not None:
            kwargs["temperature"] = m.temperature
        if m.reasoning_effort:
            kwargs["reasoning_effort"] = m.reasoning_effort
        if m.extra_body:
            kwargs["extra_body"] = m.extra_body
        _throttle(m.label, m.min_interval_s)
        try:
            resp = m.client.chat.completions.create(**kwargs)
        except m._openai.APIError as exc:
            raise ModelError(f"{exc.__class__.__name__}: {exc}") from exc

        choice = resp.choices[0]
        msg = choice.message
        self.messages.append(msg.model_dump(mode="json", exclude_none=True))
        calls: list[ToolCall] = []
        for tc in msg.tool_calls or []:
            raw = tc.function.arguments or ""
            try:
                args = json.loads(raw) if raw else {}
                err = None if isinstance(args, dict) else "arguments are not an object"
                if err:
                    args = None
            except json.JSONDecodeError as exc:
                args, err = None, f"invalid JSON: {exc}"
            calls.append(ToolCall(tc.id, tc.function.name, args, raw_arguments=raw, parse_error=err))
        raw = resp.model_dump(mode="json")
        usage = raw.get("usage") or {}
        extra = {}
        refusal = getattr(msg, "refusal", None)
        if refusal:
            extra["refusal"] = refusal
        # OpenRouter reports which upstream provider served the request.
        if raw.get("provider"):
            extra["upstream_provider"] = raw["provider"]
        cost = usage.get("cost")
        return Turn(
            text=msg.content or "",
            tool_calls=calls,
            stop_reason=str(choice.finish_reason),
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            cached_input_tokens=int((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0),
            reasoning_tokens=int((usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0),
            reported_cost=float(cost) if cost is not None else None,
            model_version=resp.model,
            raw=raw,
            extra=extra,
        )

    def add_tool_results(self, results: list[ToolResult]) -> None:
        for r in results:
            self.messages.append({"role": "tool", "tool_call_id": r.call_id, "content": r.content})


_last_call: dict[str, float] = {}
_throttle_lock = threading.Lock()


def _throttle(label: str, min_interval_s: float) -> None:
    if min_interval_s <= 0:
        return
    with _throttle_lock:
        now = time.monotonic()
        wait = _last_call.get(label, 0.0) + min_interval_s - now
        if wait > 0:
            time.sleep(wait)
        _last_call[label] = time.monotonic()


OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


class OpenRouterModel(OpenAIModel):
    """OpenRouter: one key, many models (including free ``:free`` variants).

    Uses OpenRouter's OpenAI-compatible endpoint with ``OPENROUTER_API_KEY``.
    Asks OpenRouter to include usage accounting, so each response reports the
    tokens and the cost OpenRouter charged (0 for free models). The upstream
    provider that served each request is logged, because OpenRouter may route
    the same model id to different providers.
    """

    provider = "openrouter"

    def __init__(self, label: str, model: str, **kwargs: object) -> None:
        kwargs.setdefault("base_url", OPENROUTER_BASE_URL)
        kwargs.setdefault("api_key_env", "OPENROUTER_API_KEY")
        kwargs.setdefault("max_tokens_param", "max_tokens")
        extra = dict(kwargs.pop("extra_body", None) or {})  # type: ignore[arg-type]
        extra.setdefault("usage", {"include": True})
        super().__init__(label=label, model=model, extra_body=extra, **kwargs)  # type: ignore[arg-type]
