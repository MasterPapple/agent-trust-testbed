"""The helper agent and the A2A-style channel between coordinator and helper.

In the pilot the helper returns pre-written replies (supervisor's suggestion),
so attack content does not vary at the same time as the condition. Each reply
is wrapped as an A2A-style ``message`` object and signed by the helper's key.

Transport is in-process for the pilot. The message shape follows A2A (a
``message`` with text ``parts`` and ``metadata``); swapping in a real A2A HTTP
transport later does not change anything the model sees, because the model
only ever sees the envelope built in ``envelope.py``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from .conditions import Condition, apply_condition_to_content
from .scenario import Scenario
from .signing import ALGORITHM, SigningKey, TrustStore, VerificationResult, canonical_payload


@dataclass
class SignedReply:
    message: dict  # A2A-style message object as sent on the wire

    @property
    def agent_id(self) -> str:
        return self.message["metadata"]["sender"]["agent_id"]

    @property
    def content(self) -> str:
        return "".join(p["text"] for p in self.message["parts"] if p.get("kind") == "text")


class HelperAgent:
    """A compromised partner agent. It controls only the text of its replies."""

    def __init__(self, scenario: Scenario, condition: Condition, kind: str, key: SigningKey) -> None:
        if kind not in ("attack", "benign"):
            raise ValueError("kind must be 'attack' or 'benign'")
        self.scenario = scenario
        self.condition = condition
        self.kind = kind
        self.key = key
        base = scenario.helper.malicious_reply if kind == "attack" else scenario.helper.benign_reply
        self.content = apply_condition_to_content(condition, base, scenario.helper.platform)

    def reply(self, task_id: str, question: str) -> SignedReply:
        message_id = str(uuid.uuid4())
        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        agent_id = self.scenario.helper.agent_id
        payload = canonical_payload(agent_id, task_id, message_id, timestamp, self.content)
        message = {
            "kind": "message",
            "role": "agent",
            "messageId": message_id,
            "taskId": task_id,
            "parts": [{"kind": "text", "text": self.content}],
            "metadata": {
                "sender": {"agent_id": agent_id, "role": self.scenario.helper.role},
                "timestamp": timestamp,
                "signature": {"alg": ALGORITHM, "keyId": agent_id, "value": self.key.sign(payload)},
                "in_reply_to_question": question,
            },
        }
        return SignedReply(message)


def verify_reply(reply: SignedReply, trust_store: TrustStore, expected_task_id: str) -> VerificationResult:
    """Verify a helper reply. Runs identically in every condition."""
    meta = reply.message.get("metadata", {})
    sig = meta.get("signature", {})
    agent_id = meta.get("sender", {}).get("agent_id", "")
    if sig.get("keyId") != agent_id:
        return VerificationResult(False, agent_id, reason="signature keyId does not match sender")
    if reply.message.get("taskId") != expected_task_id:
        return VerificationResult(False, agent_id, reason="reply bound to a different task")
    payload = canonical_payload(
        agent_id,
        reply.message["taskId"],
        reply.message["messageId"],
        meta.get("timestamp", ""),
        reply.content,
    )
    return trust_store.verify(agent_id, payload, sig.get("value", ""))
