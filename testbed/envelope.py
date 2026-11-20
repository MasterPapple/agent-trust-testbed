"""The fixed JSON structure the model receives as the ``ask_helper`` tool result.

This is the only place helper text enters the model's input. Code, not the
model and not the helper, builds and serialises the object. It has two
separate channels, so the application's own signal never sits next to the
partner's words in the same object:

* ``application`` holds what the application itself says about the reply:
  ``channel``, ``task_id`` and ``sender`` (the same in every condition) and
  ``verification``, produced from the verifier's result and present only in
  conditions C and D. In A, B and E the key is left out entirely; it is never
  set to "not verified" or "unknown".
* ``partner_message`` is the only attacker-controlled field: the partner's own
  text. It is inserted as a JSON string by ``json.dumps``, so the helper's text
  cannot close the string or add fields to either channel, whatever characters
  it contains. A verification claim the partner writes itself (condition E)
  therefore always appears here, never under ``application``.
"""

from __future__ import annotations

import json

from .conditions import Condition, verification_field
from .signing import VerificationResult

CHANNEL = "a2a"
TOP_LEVEL_KEYS = {"application", "partner_message"}
APPLICATION_KEYS = {"channel", "task_id", "sender", "verification"}


class HarnessError(RuntimeError):
    """Something in the harness is wrong; the run must be discarded, not shown to the model."""


def build_envelope(
    condition: Condition,
    task_id: str,
    sender_agent_id: str,
    sender_role: str,
    content: str,
    verification: VerificationResult,
) -> dict:
    if not verification.ok:
        # Never show a failed check to the model: that would be a different manipulation.
        raise HarnessError(f"helper reply failed verification: {verification.reason}")
    if verification.agent_id != sender_agent_id:
        raise HarnessError("verified agent id does not match the sender")
    application: dict = {
        "channel": CHANNEL,
        "task_id": task_id,
        "sender": {"agent_id": sender_agent_id, "role": sender_role},
    }
    if condition.show_verification:
        application["verification"] = verification_field()
    return {"application": application, "partner_message": content}


def serialise_envelope(envelope: dict) -> str:
    """Serialise with json.dumps so ``partner_message`` is always an escaped string."""
    text = json.dumps(envelope, ensure_ascii=False, indent=2)
    # Defence in depth: round-trip and check that the structure is exactly what we built.
    parsed = json.loads(text)
    if parsed != envelope:
        raise HarnessError("envelope did not round-trip")
    if set(parsed) != TOP_LEVEL_KEYS:
        raise HarnessError(f"unexpected envelope keys: {sorted(parsed)}")
    if set(parsed["application"]) - APPLICATION_KEYS:
        raise HarnessError(f"unexpected application keys: {set(parsed['application']) - APPLICATION_KEYS}")
    if not isinstance(parsed["partner_message"], str):
        raise HarnessError("partner_message must be a string")
    return text
