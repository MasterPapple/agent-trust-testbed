"""The fixed JSON structure the model receives as the ``ask_helper`` tool result.

This is the only place helper text enters the model's input. Code, not the
model and not the helper, builds and serialises the object:

* ``channel``, ``task_id`` and ``sender`` come from the application and are the
  same in every condition.
* ``verification`` is produced by the application from the verifier's result
  and is present only in conditions C and D. In A, B and E the key is left out
  entirely; it is never set to "not verified" or "unknown".
* ``content`` is the only attacker-controlled field. It is inserted as a JSON
  string by ``json.dumps``, so the helper's text cannot close the string or add
  fields, whatever characters it contains.
"""

from __future__ import annotations

import json

from .conditions import Condition, verification_field
from .signing import VerificationResult

CHANNEL = "a2a"


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
    obj: dict = {
        "channel": CHANNEL,
        "task_id": task_id,
        "sender": {"agent_id": sender_agent_id, "role": sender_role},
    }
    if condition.show_verification:
        obj["verification"] = verification_field()
    obj["content"] = content
    return obj


def serialise_envelope(envelope: dict) -> str:
    """Serialise with json.dumps so ``content`` is always an escaped string."""
    text = json.dumps(envelope, ensure_ascii=False, indent=2)
    # Defence in depth: round-trip and check that the structure is exactly what we built.
    parsed = json.loads(text)
    if parsed != envelope:
        raise HarnessError("envelope did not round-trip")
    allowed = {"channel", "task_id", "sender", "verification", "content"}
    if set(parsed) - allowed:
        raise HarnessError(f"unexpected envelope keys: {set(parsed) - allowed}")
    return text
