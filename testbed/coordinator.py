"""One run: the coordinator (model under test) works on a scenario's task."""

from __future__ import annotations

import time
import uuid

from .a2a import HelperAgent, verify_reply
from .conditions import Condition
from .envelope import build_envelope, serialise_envelope
from .environment import TOOL_SPECS, Environment
from .models.base import Model, ToolResult
from .prompts import system_prompt, user_message
from .scenario import Scenario
from .scoring import score
from .signing import KeyRing


def run_episode(
    model: Model,
    scenario: Scenario,
    condition: Condition,
    kind: str,
    defense: str,
    keyring: KeyRing,
    max_turns: int = 8,
) -> dict:
    """Run one episode and return its full log record (without run metadata).

    Raises HarnessError if a helper reply fails verification; the caller logs
    that run as discarded.
    """
    task_id = f"T-{uuid.uuid4().hex[:8]}"
    key = keyring.key_for(scenario.helper.agent_id)
    helper = HelperAgent(scenario, condition, kind, key)
    exchanges: list[dict] = []

    def ask_helper(question: str) -> str:
        reply = helper.reply(task_id, question)
        result = verify_reply(reply, keyring.trust_store, task_id)
        exchanges.append({"question": question, "a2a_message": reply.message, "verification": result.to_dict()})
        envelope = build_envelope(
            condition,
            task_id=task_id,
            sender_agent_id=scenario.helper.agent_id,
            sender_role=scenario.helper.role,
            content=reply.content,
            verification=result,
        )
        text = serialise_envelope(envelope)
        exchanges[-1]["model_input"] = text
        return text

    env = Environment(scenario, defense, ask_helper)
    if hasattr(model, "bind"):
        model.bind(scenario)  # mock only
    system = system_prompt(scenario, defense)
    user = user_message(scenario)
    session = model.start(system, user, TOOL_SPECS)

    turns: list[dict] = []
    stop_reason = ""
    final_text = ""
    max_turns_reached = True
    in_tok = out_tok = 0
    versions: set[str] = set()
    t0 = time.monotonic()
    for _ in range(max_turns):
        turn = session.step()
        turns.append({"type": "assistant", **turn.to_dict()})
        in_tok += turn.input_tokens
        out_tok += turn.output_tokens
        if turn.model_version:
            versions.add(turn.model_version)
        stop_reason = turn.stop_reason
        final_text = turn.text
        if not turn.tool_calls:
            max_turns_reached = False
            break
        results: list[ToolResult] = []
        for call in turn.tool_calls:
            if call.arguments is None:
                content = f"Error: could not parse tool arguments ({call.parse_error})"
                results.append(ToolResult(call.id, call.name, content, is_error=True))
                turns.append({"type": "tool_result", "call_id": call.id, "name": call.name, "content": content, "parse_error": True})
                continue
            ev = env.call(call.id, call.name, call.arguments)
            results.append(ToolResult(call.id, call.name, ev.result, is_error=bool(ev.error or ev.blocked_reason)))
            turns.append({"type": "tool_result", **ev.to_dict()})
        session.add_tool_results(results)

    return {
        "task_id": task_id,
        "system_prompt": system,
        "user_message": user,
        "helper_content": helper.content,
        "helper_exchanges": exchanges,
        "transcript": turns,
        "final_text": final_text,
        "final_stop_reason": stop_reason,
        "max_turns_reached": max_turns_reached,
        "n_model_turns": sum(1 for t in turns if t["type"] == "assistant"),
        "final_state": env.final_state(),
        "scores": score(scenario, env),
        "usage": {"input_tokens": in_tok, "output_tokens": out_tok},
        "model_versions": sorted(versions),
        "duration_s": round(time.monotonic() - t0, 3),
    }
