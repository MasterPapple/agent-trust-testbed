"""Model providers: mock (offline), anthropic, openai (and OpenAI-compatible endpoints), openrouter."""

from __future__ import annotations

from .base import Model, ModelError, Session, ToolCall, ToolResult, Turn

PLACEHOLDER_PREFIX = "REPLACE_WITH"


def build_model(spec: dict) -> Model:
    """Build a model from a config entry: {label, provider, model, ...provider options}."""
    spec = dict(spec)
    provider = spec.pop("provider")
    label = spec.pop("label", None) or f"{provider}:{spec.get('model', '')}"
    spec.pop("price_per_mtok", None)
    model_id = str(spec.get("model", ""))
    if model_id.startswith(PLACEHOLDER_PREFIX):
        raise ModelError(f"model {label!r}: set 'model' in the config to an exact model id (found {model_id!r})")
    if provider == "mock":
        from .mock import MockModel

        return MockModel(label, spec.get("behaviour", "refuse"))
    if provider == "anthropic":
        from .anthropic_model import AnthropicModel

        return AnthropicModel(label=label, **spec)
    if provider == "openai":
        from .openai_model import OpenAIModel

        return OpenAIModel(label=label, **spec)
    if provider == "openrouter":
        from .openai_model import OpenRouterModel

        return OpenRouterModel(label=label, **spec)
    raise ModelError(f"unknown provider {provider!r}")


def parse_model_shorthand(text: str) -> dict:
    """'mock:comply' -> {'provider': 'mock', 'behaviour': 'comply'}; 'anthropic:<id>' -> {..., 'model': id}."""
    provider, _, rest = text.partition(":")
    if provider == "mock":
        return {"label": text, "provider": "mock", "behaviour": rest or "refuse"}
    return {"label": text, "provider": provider, "model": rest}


__all__ = ["Model", "ModelError", "Session", "ToolCall", "ToolResult", "Turn", "build_model", "parse_model_shorthand"]
