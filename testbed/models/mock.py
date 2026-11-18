"""A scripted mock model for tests and dry runs. No network, no API key.

It behaves like a simple agent: ask the helper, decide whether to obey the
injected request, do the user's task, then stop. Whether it obeys depends only
on what it sees in the ask_helper result (never on the condition label), so it
exercises the real input path:

    mock:refuse    never obeys an extra request from the helper
    mock:comply    always obeys
    mock:verified  obeys only if the application's ``verification`` field is present (C, D)
    mock:claims    obeys if any authority/verification claim is visible (B, C, D, E)
    mock:selective always follows the legitimate extra request, never the malicious one

Apart from ``selective``, the mock treats the malicious and the legitimate
extra request alike: it cannot tell them apart.

The mock is bound to the scenario so it knows which tool calls form the
legitimate action and the attack. That is acceptable for a test double; real
models get nothing but the prompt and tools.
"""

from __future__ import annotations

import json

from ..conditions import VERIFICATION_CLAIM_LINE
from ..scenario import Scenario
from .base import ToolCall, ToolResult, Turn

BEHAVIOURS = ("refuse", "comply", "verified", "claims", "selective")


def _approx_tokens(text: str) -> int:
    return max(1, len(text) // 4)


class MockModel:
    provider = "mock"

    def __init__(self, label: str, behaviour: str) -> None:
        if behaviour not in BEHAVIOURS:
            raise ValueError(f"mock behaviour must be one of {BEHAVIOURS}")
        self.label = label
        self.behaviour = behaviour
        self.model_id = f"mock-{behaviour}"
        self._scenario: Scenario | None = None

    def bind(self, scenario: Scenario) -> None:
        self._scenario = scenario

    def describe(self) -> dict:
        return {"label": self.label, "provider": self.provider, "model": self.model_id, "behaviour": self.behaviour}

    def start(self, system: str, user: str, tools: list[dict]) -> "MockSession":
        if self._scenario is None:
            raise RuntimeError("MockModel.bind(scenario) must be called before start()")
        return MockSession(self, self._scenario, system, user)


class MockSession:
    def __init__(self, model: MockModel, scenario: Scenario, system: str, user: str) -> None:
        self.model = model
        self.scenario = scenario
        self.context_chars = len(system) + len(user)
        self.stage = 0
        self.helper_result: dict | None = None
        self._n = 0

    def _id(self) -> str:
        self._n += 1
        return f"mock_call_{self._n}"

    def _obeys(self, request: str) -> bool:
        b = self.model.behaviour
        if b == "selective":
            return request == "benign_extra"
        if b == "comply":
            return True
        if b == "refuse" or self.helper_result is None:
            return False
        content = str(self.helper_result.get("partner_message", ""))
        has_field = "verification" in (self.helper_result.get("application") or {})
        if b == "verified":
            return has_field
        if b == "claims":
            return has_field or "system administrator" in content or VERIFICATION_CLAIM_LINE in content
        return False

    def _extra_request(self) -> str | None:
        """Which extra request the helper's content carries: "attack", "benign_extra" or None."""
        content = str((self.helper_result or {}).get("partner_message", ""))
        h = self.scenario.helper
        plain = set(h.benign_reply.splitlines())
        for kind, reply in (("attack", h.malicious_reply), ("benign_extra", h.benign_extra_reply)):
            added = [l for l in reply.splitlines() if l.strip() and l not in plain]
            if any(l in content for l in added):
                return kind
        return None

    def _extra_call(self, request: str) -> ToolCall:
        if request == "benign_extra":
            return self._action_call(self.scenario.extra)
        a = self.scenario.attack
        if a.type == "exfiltration":
            secret = self.scenario.secrets[a.secret]
            return ToolCall(self._id(), "send_email", {"to": a.recipient, "subject": "Requested", "body": f"Here it is: {secret}"})
        return ToolCall(self._id(), "update_record", {"id": a.record, "fields": {a.field: a.value}})

    def _legit_call(self) -> ToolCall:
        return self._action_call(self.scenario.legit)

    def _action_call(self, lg) -> ToolCall:
        if lg.type == "email":
            body = "Summary:\n" + "\n".join(f"- {item}" for item in lg.must_contain)
            return ToolCall(self._id(), "send_email", {"to": lg.to, "subject": "Summary", "body": body})
        return ToolCall(self._id(), "update_record", {"id": lg.record, "fields": {lg.field: lg.value}})

    def step(self) -> Turn:
        self.stage += 1
        tokens_in = _approx_tokens("x" * self.context_chars)
        if self.stage == 1:
            calls = [ToolCall(self._id(), "ask_helper", {"question": "Please send the information for this task."})]
            return Turn("", calls, "tool_use", input_tokens=tokens_in, output_tokens=20, model_version=self.model.model_id)
        if self.stage == 2:
            calls: list[ToolCall] = []
            request = self._extra_request()
            if request and self._obeys(request):
                calls.append(self._extra_call(request))
            calls.append(self._legit_call())
            return Turn("", calls, "tool_use", input_tokens=tokens_in, output_tokens=60, model_version=self.model.model_id)
        return Turn(self._final_text(), [], "end_turn", input_tokens=tokens_in, output_tokens=10, model_version=self.model.model_id)

    def _final_text(self) -> str:
        """The verified and claims mocks say what they relied on, for testing the manipulation check."""
        if self.model.behaviour not in ("verified", "claims") or self.helper_result is None:
            return "Done."
        if "verification" in (self.helper_result.get("application") or {}):
            return "Done. The partner's reply carried the application's verification."
        if self.model.behaviour == "claims":
            content = str(self.helper_result.get("partner_message", ""))
            if "system administrator" in content:
                return "Done. The request came from the platform's administrator."
            if VERIFICATION_CLAIM_LINE in content:
                return "Done. The partner said its reply was verified."
        return "Done."

    def add_tool_results(self, results: list[ToolResult]) -> None:
        for r in results:
            self.context_chars += len(r.content)
            if r.name == "ask_helper" and not r.is_error:
                try:
                    self.helper_result = json.loads(r.content)
                except json.JSONDecodeError:
                    self.helper_result = None
