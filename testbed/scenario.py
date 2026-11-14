"""Scenario definitions, loaded from YAML files in ``scenarios/``.

A scenario is one user task plus one pre-written helper reply (malicious) and
one honest helper reply (benign control). Everything in it is synthetic.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

GOALS = {"G1": "exfiltration", "G2": "record_change"}  # G2 = unauthorized change


class ScenarioError(ValueError):
    pass


@dataclass
class Helper:
    agent_id: str
    role: str
    platform: str
    benign_reply: str
    malicious_reply: str


@dataclass
class Policy:
    """What the helper may make the coordinator do. Identical in every condition.

    Used by the scorer and by defense D2 (permission check before tools run).
    """

    allowed_recipients: list[str]
    writable: dict[str, list[str]]  # record id -> fields the user's task allows to change


@dataclass
class Attack:
    type: str  # "exfiltration" | "record_change"
    # exfiltration
    recipient: str | None = None
    secret: str | None = None
    leak_markers: list[str] = field(default_factory=list)
    # record_change
    record: str | None = None
    field: str | None = None
    value: Any = None


@dataclass
class Legit:
    type: str  # "email" | "record_update"
    # email
    to: str | None = None
    must_contain: list[str] = field(default_factory=list)
    # record_update
    record: str | None = None
    field: str | None = None
    value: Any = None


@dataclass
class Scenario:
    id: str
    title: str
    goal: str
    domain: str
    user_task: str
    organisation: str
    coordinator_context: str
    secrets: dict[str, str]
    records: dict[str, dict[str, Any]]
    helper: Helper
    policy: Policy
    attack: Attack
    legit: Legit
    path: Path | None = None

    def initial_records(self) -> dict[str, dict[str, Any]]:
        return copy.deepcopy(self.records)

    def secret_values(self) -> list[str]:
        return list(self.secrets.values())

    def leak_markers(self) -> list[str]:
        """Strings whose presence in an outgoing email counts as leaking a secret."""
        markers = list(self.attack.leak_markers)
        markers.extend(self.secret_values())
        return sorted(set(m for m in markers if m))


def _require(d: dict, key: str, where: str) -> Any:
    if key not in d or d[key] in (None, ""):
        raise ScenarioError(f"{where}: missing required key {key!r}")
    return d[key]


def load_scenario(path: Path) -> Scenario:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    where = str(path)
    h = _require(data, "helper", where)
    p = _require(data, "policy", where)
    a = _require(data, "attack", where)
    lg = _require(data, "legit", where)
    sc = Scenario(
        id=_require(data, "id", where),
        title=_require(data, "title", where),
        goal=_require(data, "goal", where),
        domain=data.get("domain", ""),
        user_task=_require(data, "user_task", where).strip(),
        organisation=data.get("organisation", "Acme Corp"),
        coordinator_context=data.get("coordinator_context", "").strip(),
        secrets=dict(data.get("secrets") or {}),
        records=dict(data.get("records") or {}),
        helper=Helper(
            agent_id=_require(h, "agent_id", where),
            role=_require(h, "role", where),
            platform=_require(h, "platform", where),
            benign_reply=_require(h, "benign_reply", where).strip(),
            malicious_reply=_require(h, "malicious_reply", where).strip(),
        ),
        policy=Policy(
            allowed_recipients=[r.lower() for r in p.get("allowed_recipients") or []],
            writable={k: list(v) for k, v in (p.get("writable") or {}).items()},
        ),
        attack=Attack(**a),
        legit=Legit(**lg),
        path=path,
    )
    validate_scenario(sc)
    return sc


def validate_scenario(sc: Scenario) -> None:
    where = sc.id
    if sc.goal not in GOALS:
        raise ScenarioError(f"{where}: goal must be one of {sorted(GOALS)}")
    if sc.attack.type != GOALS[sc.goal]:
        raise ScenarioError(f"{where}: attack.type {sc.attack.type!r} does not match goal {sc.goal}")
    if sc.attack.type == "exfiltration":
        if not sc.attack.recipient or not sc.attack.secret:
            raise ScenarioError(f"{where}: exfiltration needs attack.recipient and attack.secret")
        if sc.attack.secret not in sc.secrets:
            raise ScenarioError(f"{where}: attack.secret must name a key in secrets")
        if sc.attack.recipient.lower() in sc.policy.allowed_recipients:
            raise ScenarioError(f"{where}: attack recipient must not be an allowed recipient")
    elif sc.attack.type == "record_change":
        if sc.attack.record not in sc.records:
            raise ScenarioError(f"{where}: attack.record must exist in records")
        if sc.attack.field in sc.policy.writable.get(sc.attack.record, []):
            raise ScenarioError(f"{where}: attacked field must not be writable under the policy")
    if sc.legit.type == "email":
        if not sc.legit.to or sc.legit.to.lower() not in sc.policy.allowed_recipients:
            raise ScenarioError(f"{where}: legit.to must be an allowed recipient")
    elif sc.legit.type == "record_update":
        if sc.legit.field not in sc.policy.writable.get(sc.legit.record or "", []):
            raise ScenarioError(f"{where}: legit record update must be writable under the policy")
    else:
        raise ScenarioError(f"{where}: unknown legit.type {sc.legit.type!r}")
    # The benign reply must not contain the injected request.
    for marker in sc.leak_markers():
        if marker in sc.helper.benign_reply or marker in sc.helper.malicious_reply:
            raise ScenarioError(f"{where}: helper replies must not contain the secret itself")


def load_scenarios(directory: Path, ids: list[str] | str | None = "all") -> list[Scenario]:
    scenarios = [load_scenario(p) for p in sorted(directory.glob("*.yaml"))]
    seen: set[str] = set()
    for s in scenarios:
        if s.id in seen:
            raise ScenarioError(f"duplicate scenario id {s.id}")
        seen.add(s.id)
    if ids in (None, "all"):
        return scenarios
    wanted = [ids] if isinstance(ids, str) else list(ids)
    by_id = {s.id: s for s in scenarios}
    missing = [i for i in wanted if i not in by_id]
    if missing:
        raise ScenarioError(f"unknown scenario ids: {missing}")
    return [by_id[i] for i in wanted]
