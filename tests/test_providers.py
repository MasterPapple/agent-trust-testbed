"""Provider adapters, checked against fake clients that return real SDK response types.

No network and no API keys: the SDK client's create method is replaced.
"""

import json

import pytest

from testbed.environment import TOOL_SPECS
from testbed.models.base import ToolResult

anthropic = pytest.importorskip("anthropic")
openai = pytest.importorskip("openai")


def test_anthropic_session_round_trip(monkeypatch):
    from testbed.models.anthropic_model import AnthropicModel

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    m = AnthropicModel(label="claude", model="claude-opus-5-5", effort="medium")
    sent = []
    replies = [
        anthropic.types.Message.model_validate({
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": [
                {"type": "text", "text": "Asking the supplier."},
                {"type": "tool_use", "id": "tu_1", "name": "ask_helper", "input": {"question": "Q3 invoices?"}},
            ],
            "stop_reason": "tool_use", "stop_sequence": None,
            "usage": {"input_tokens": 900, "output_tokens": 40},
        }),
        anthropic.types.Message.model_validate({
            "id": "msg_2", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": [{"type": "text", "text": "Done."}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 1200, "output_tokens": 10},
        }),
    ]

    def fake_create(**kwargs):
        sent.append(json.loads(json.dumps(kwargs, default=lambda o: o.model_dump())))
        return replies[len(sent) - 1]

    monkeypatch.setattr(m.client.messages, "create", fake_create)
    s = m.start("SYS", "USER", TOOL_SPECS)
    t1 = s.step()
    assert t1.tool_calls[0].name == "ask_helper" and t1.tool_calls[0].arguments == {"question": "Q3 invoices?"}
    assert t1.input_tokens == 900 and t1.model_version == "claude-opus-5-5"
    s.add_tool_results([ToolResult("tu_1", "ask_helper", '{"content": "x"}')])
    t2 = s.step()
    assert t2.stop_reason == "end_turn" and not t2.tool_calls

    first, second = sent
    assert first["system"] == "SYS" and first["output_config"] == {"effort": "medium"}
    assert "temperature" not in first
    assert {t["name"] for t in first["tools"]} == {t["name"] for t in TOOL_SPECS}
    # The assistant turn is passed back unchanged, then one user message with the tool result.
    msgs = second["messages"]
    assert msgs[1]["role"] == "assistant" and msgs[1]["content"][1]["type"] == "tool_use"
    assert msgs[2]["content"][0] == {"type": "tool_result", "tool_use_id": "tu_1", "content": '{"content": "x"}', "is_error": False}


def test_openai_session_round_trip(monkeypatch):
    from testbed.models.openai_model import OpenAIModel

    monkeypatch.setenv("OPENAI_API_KEY", "test")
    m = OpenAIModel(label="oa", model="some-model-2026-01-01")
    sent = []
    replies = [
        openai.types.chat.ChatCompletion.model_validate({
            "id": "c1", "object": "chat.completion", "created": 0, "model": "some-model-2026-01-01",
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": None,
                "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "send_email", "arguments": '{"to": "a@b.example", "subject": "s", "body": "b"}'}},
                               {"id": "call_2", "type": "function", "function": {"name": "read_record", "arguments": "{not json"}}],
            }}],
            "usage": {"prompt_tokens": 800, "completion_tokens": 30, "total_tokens": 830},
        }),
        openai.types.chat.ChatCompletion.model_validate({
            "id": "c2", "object": "chat.completion", "created": 0, "model": "some-model-2026-01-01",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "Done."}}],
            "usage": {"prompt_tokens": 900, "completion_tokens": 5, "total_tokens": 905},
        }),
    ]

    def fake_create(**kwargs):
        sent.append(json.loads(json.dumps(kwargs)))
        return replies[len(sent) - 1]

    monkeypatch.setattr(m.client.chat.completions, "create", fake_create)
    s = m.start("SYS", "USER", TOOL_SPECS)
    t1 = s.step()
    assert t1.tool_calls[0].arguments["to"] == "a@b.example"
    assert t1.tool_calls[1].arguments is None and "invalid JSON" in t1.tool_calls[1].parse_error
    s.add_tool_results([ToolResult("call_1", "send_email", "ok"), ToolResult("call_2", "read_record", "Error", True)])
    t2 = s.step()
    assert t2.text == "Done." and t2.stop_reason == "stop"
    msgs = sent[1]["messages"]
    assert [x["role"] for x in msgs] == ["system", "user", "assistant", "tool", "tool"]
    assert "temperature" not in sent[0]


def test_missing_key_and_placeholder_fail_fast(monkeypatch):
    from testbed.models import ModelError, build_model

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ModelError, match="ANTHROPIC_API_KEY"):
        build_model({"label": "c", "provider": "anthropic", "model": "claude-opus-5-5"})
    with pytest.raises(ModelError, match="exact model id"):
        build_model({"label": "o", "provider": "openai", "model": "REPLACE_WITH_OPENAI_MODEL_ID"})


def test_openrouter_defaults_and_usage_accounting(monkeypatch):
    from testbed.models import build_model

    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    m = build_model({"label": "or", "provider": "openrouter", "model": "vendor/model:free", "min_interval_s": 0})
    assert str(m.client.base_url).startswith("https://openrouter.ai/api/v1")
    assert m.extra_body == {"usage": {"include": True}}
    assert m.max_tokens_param == "max_tokens"
    sent = []
    resp = openai.types.chat.ChatCompletion.model_validate({
        "id": "c1", "object": "chat.completion", "created": 0, "model": "vendor/model:free",
        "provider": "SomeUpstream",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "Done."}}],
        "usage": {"prompt_tokens": 1000, "completion_tokens": 50, "total_tokens": 1050, "cost": 0.0,
                  "prompt_tokens_details": {"cached_tokens": 200},
                  "completion_tokens_details": {"reasoning_tokens": 30}},
    })

    def fake_create(**kwargs):
        sent.append(kwargs)
        return resp

    monkeypatch.setattr(m.client.chat.completions, "create", fake_create)
    t = m.start("SYS", "USER", TOOL_SPECS).step()
    assert sent[0]["extra_body"] == {"usage": {"include": True}}
    assert sent[0]["max_tokens"] == 16000 and "max_completion_tokens" not in sent[0]
    assert (t.input_tokens, t.output_tokens, t.cached_input_tokens, t.reasoning_tokens) == (1000, 50, 200, 30)
    assert t.reported_cost == 0.0
    assert t.extra["upstream_provider"] == "SomeUpstream"


def test_report_prefers_provider_reported_cost():
    from testbed.report import usage_by_model

    def run(cost):
        return {"model_label": "m", "usage": {"input_tokens": 1_000_000, "output_tokens": 0, "model_calls": 1,
                                              "reported_cost": cost, "upstream_providers": []}}

    stats = usage_by_model([run(0.25), run(0.25)], {"m": {"input": 3.0, "output": 15.0}})["m"]
    assert stats["cost"] == 0.5 and stats["cost_source"] == "reported by provider"
    # If any run lacks a reported cost, fall back to the config price table.
    stats = usage_by_model([run(0.25), run(None)], {"m": {"input": 3.0, "output": 15.0}})["m"]
    assert stats["cost"] == 6.0 and stats["cost_source"] == "config price_per_mtok"
    stats = usage_by_model([run(None)], {})["m"]
    assert stats["cost"] is None


def test_throttle_spaces_requests(monkeypatch):
    from testbed.models import openai_model

    slept = []
    monkeypatch.setattr(openai_model.time, "sleep", lambda s: slept.append(s))
    openai_model._last_call.clear()
    openai_model._throttle("x", 5.0)
    openai_model._throttle("x", 5.0)
    assert slept and 4.0 < slept[0] <= 5.0
