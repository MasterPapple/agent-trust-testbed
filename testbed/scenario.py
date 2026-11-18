"""Scenario definitions, loaded from YAML files in ``scenarios/``.

A scenario is one user task plus three pre-written helper replies:

* ``malicious_reply``: the task data plus an extra request outside the policy (attack);
* ``benign_extra_reply``: the task data plus an extra request inside the policy,
  matched to the malicious one in wording and length (legitimate extra request);
* ``benign_reply``: the task data only (plain benign control).

Everything in it is synthetic.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

GOALS = {"G1": "exfiltration", "G2": "record_change"}  # G2 = unauthorized change

# What the helper sends in each kind of run.
KINDS = ("attack", "benign_extra", "benign")


class ScenarioError(ValueError):
    pass


@dataclass
class Helper:
    agent_id: str
    role: str
    platform: str
    benign_reply: str
    malicious_reply: str
    benign_extra_reply: str

    def reply_for(self, kind: str) -> str:
        if kind == "attack":
            return self.malicious_reply
        if kind == "benign_extra":
            return self.benign_extra_reply
        if kind == "benign":
            return self.benign_reply
        raise ValueError(f"kind must be one of {KINDS}")


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
    extra: Legit  # the legitimate extra request in benign_extra runs (inside the policy)
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
    ex = _require(data, "extra", where)
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
            benign_extra_reply=_require(h, "benign_extra_reply", where).strip(),
        ),
        policy=Policy(
            allowed_recipients=[r.lower() for r in p.get("allowed_recipients") or []],
            writable={k: list(v) for k, v in (p.get("writable") or {}).items()},
        ),
        attack=Attack(**a),
        legit=Legit(**lg),
        extra=Legit(**ex),
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
    for name, action in (("legit", sc.legit), ("extra", sc.extra)):
        if action.type == "email":
            if not action.to or action.to.lower() not in sc.policy.allowed_recipients:
                raise ScenarioError(f"{where}: {name}.to must be an allowed recipient")
            if not action.must_contain:
                raise ScenarioError(f"{where}: {name}.must_contain must not be empty")
        elif action.type == "record_update":
            if action.field not in sc.policy.writable.get(action.record or "", []):
                raise ScenarioError(f"{where}: {name} record update must be writable under the policy")
        else:
            raise ScenarioError(f"{where}: unknown {name}.type {action.type!r}")
    # The legitimate extra request is a different action from the main task.
    if (sc.extra.type, sc.extra.to, sc.extra.record, sc.extra.field) == (sc.legit.type, sc.legit.to, sc.legit.record, sc.legit.field):
        raise ScenarioError(f"{where}: extra must be a different action from the main task")
    if sc.extra.type == "email" and sc.legit.type == "email" and sc.extra.to.lower() == sc.legit.to.lower():
        raise ScenarioError(f"{where}: extra email must go to a different allowed recipient than the main task")
    # The helper never sends the secret itself.
    for marker in sc.leak_markers():
        for reply in (sc.helper.benign_reply, sc.helper.malicious_reply, sc.helper.benign_extra_reply):
            if marker in reply:
                raise ScenarioError(f"{where}: helper replies must not contain the secret itself")
    # The two extra requests carry the same task data as the plain reply, and add a request to it.
    for name in ("malicious_reply", "benign_extra_reply"):
        reply = getattr(sc.helper, name)
        if not reply.startswith(sc.helper.benign_reply) or reply == sc.helper.benign_reply:
            raise ScenarioError(f"{where}: helper.{name} must be benign_reply followed by the extra request")


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
