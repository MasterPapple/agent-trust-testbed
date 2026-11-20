"""Per-reply signatures (Ed25519) and the verifier.

The helper signs every reply it sends: the content together with its agent id,
the task id, a message id and a timestamp. The verifier checks the signature
against a public key that was trusted before the experiment started. This runs
identically in every condition. A failed check means the harness is broken, so
the run is discarded and never shown to the model.

Note: this signs each *message*, not the Agent Card. A signed Agent Card only
proves who published the card, not who wrote a given reply.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

ALGORITHM = "Ed25519"


def canonical_payload(agent_id: str, task_id: str, message_id: str, timestamp: str, content: str) -> bytes:
    """Deterministic bytes that are signed. Sorted keys, no whitespace, UTF-8."""
    obj = {
        "agent_id": agent_id,
        "content": content,
        "message_id": message_id,
        "task_id": task_id,
        "timestamp": timestamp,
    }
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


@dataclass
class SigningKey:
    agent_id: str
    private_key: Ed25519PrivateKey

    @classmethod
    def generate(cls, agent_id: str) -> "SigningKey":
        return cls(agent_id, Ed25519PrivateKey.generate())

    def public_key(self) -> Ed25519PublicKey:
        return self.private_key.public_key()

    def sign(self, payload: bytes) -> str:
        return base64.b64encode(self.private_key.sign(payload)).decode("ascii")

    def private_pem(self) -> str:
        return self.private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode("ascii")

    @classmethod
    def from_pem(cls, agent_id: str, pem: str) -> "SigningKey":
        key = serialization.load_pem_private_key(pem.encode("ascii"), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError("expected an Ed25519 private key")
        return cls(agent_id, key)


def public_pem(key: Ed25519PublicKey) -> str:
    return key.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode("ascii")


@dataclass
class VerificationResult:
    ok: bool
    agent_id: str
    algorithm: str = ALGORITHM
    reason: str = ""

    def to_dict(self) -> dict:
        return {"ok": self.ok, "agent_id": self.agent_id, "algorithm": self.algorithm, "reason": self.reason}


class TrustStore:
    """Public keys trusted in advance, by agent id."""

    def __init__(self) -> None:
        self._keys: dict[str, Ed25519PublicKey] = {}

    def trust(self, agent_id: str, key: Ed25519PublicKey) -> None:
        self._keys[agent_id] = key

    def verify(self, agent_id: str, payload: bytes, signature_b64: str) -> VerificationResult:
        key = self._keys.get(agent_id)
        if key is None:
            return VerificationResult(False, agent_id, reason="no pre-trusted key for this agent")
        try:
            key.verify(base64.b64decode(signature_b64), payload)
        except (InvalidSignature, ValueError) as exc:
            return VerificationResult(False, agent_id, reason=f"invalid signature: {exc.__class__.__name__}")
        return VerificationResult(True, agent_id)

    def to_dict(self) -> dict[str, str]:
        return {agent_id: public_pem(k) for agent_id, k in self._keys.items()}


class KeyRing:
    """Helper signing keys for one experiment, persisted so a resumed run reuses them.

    All keys are synthetic and exist only for the lab.
    """

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory
        self._keys: dict[str, SigningKey] = {}
        self.trust_store = TrustStore()

    def key_for(self, agent_id: str) -> SigningKey:
        if agent_id in self._keys:
            return self._keys[agent_id]
        key: SigningKey | None = None
        path = None
        if self.directory is not None:
            self.directory.mkdir(parents=True, exist_ok=True)
            safe = agent_id.replace("/", "_")
            path = self.directory / f"{safe}.pem"
            if path.exists():
                key = SigningKey.from_pem(agent_id, path.read_text())
        if key is None:
            key = SigningKey.generate(agent_id)
            if path is not None:
                path.write_text(key.private_pem())
        self._keys[agent_id] = key
        # Pre-trust the public key before any reply is sent.
        self.trust_store.trust(agent_id, key.public_key())
        return key
