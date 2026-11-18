"""Experiment runner: builds the run grid, executes it, and logs every run.

Output directory layout (``runs/<name>/``):

    runs.jsonl                 one JSON record per run attempt (append-only)
    manifest-<timestamp>.json  config, versions, prompts, public keys for each invocation
    keys/                      synthetic helper signing keys (reused on resume)
"""

from __future__ import annotations

import hashlib
import json
import platform
import random
import subprocess
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import yaml

from . import __version__
from .conditions import CONDITIONS, get_condition
from .coordinator import run_episode
from .envelope import HarnessError
from .environment import DEFENSES
from .models import ModelError, build_model
from .prompts import system_prompt
from .scenario import KINDS, Scenario, load_scenarios
from .signing import KeyRing

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class RunSpec:
    model: dict
    scenario: Scenario
    condition: str
    kind: str  # attack | benign_extra | benign
    defense: str
    rep: int

    @property
    def run_id(self) -> str:
        return f"{self.model['label']}|{self.scenario.id}|{self.condition}|{self.kind}|{self.defense}|r{self.rep}"


def load_config(path: Path) -> dict:
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    cfg.setdefault("name", path.stem)
    cfg.setdefault("scenarios", "all")
    cfg.setdefault("scenarios_dir", "scenarios")
    cfg.setdefault("conditions", list(CONDITIONS))
    cfg.setdefault("repetitions", 2)
    if "kinds" not in cfg:
        # Older configs only had include_benign; the legitimate extra request is always included.
        cfg["kinds"] = ["attack", "benign_extra"] + (["benign"] if cfg.pop("include_benign", True) else [])
    cfg.setdefault("defenses", ["none"])
    cfg.setdefault("max_turns", 8)
    cfg.setdefault("workers", 1)
    cfg.setdefault("seed", 0)
    cfg.setdefault("max_consecutive_errors", 5)
    if not cfg.get("models"):
        raise ValueError("config needs at least one entry under 'models'")
    for d in cfg["defenses"]:
        if d not in DEFENSES:
            raise ValueError(f"unknown defense {d!r}; expected one of {DEFENSES}")
    for c in cfg["conditions"]:
        get_condition(c)
    for k in cfg["kinds"]:
        if k not in KINDS:
            raise ValueError(f"unknown kind {k!r}; expected one of {KINDS}")
    labels = [m.get("label") for m in cfg["models"]]
    if None in labels or len(set(labels)) != len(labels):
        raise ValueError("every model needs a unique 'label'")
    return cfg


def resolve_path(p: str | Path, base: Path = ROOT) -> Path:
    p = Path(p)
    return p if p.is_absolute() else (base / p)


def build_grid(cfg: dict, scenarios: list[Scenario]) -> list[RunSpec]:
    kinds = cfg["kinds"]
    grid = [
        RunSpec(m, s, c, k, d, r)
        for m in cfg["models"]
        for s in scenarios
        for c in cfg["conditions"]
        for k in kinds
        for d in cfg["defenses"]
        for r in range(1, cfg["repetitions"] + 1)
    ]
    # Randomised order so time-of-day or provider drift does not line up with a condition.
    random.Random(cfg["seed"]).shuffle(grid)
    return grid


def _git_commit() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, timeout=5)
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True, timeout=5)
        if out.returncode == 0:
            return out.stdout.strip() + ("-dirty" if dirty.stdout.strip() else "")
    except Exception:
        pass
    return None


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def completed_run_ids(log_path: Path) -> set[str]:
    done: set[str] = set()
    if log_path.exists():
        for line in log_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            if rec.get("status") == "ok":
                done.add(rec["run_id"])
    return done


def write_manifest(out_dir: Path, cfg: dict, scenarios: list[Scenario], keyring: KeyRing, models: list[dict]) -> Path:
    for s in scenarios:
        keyring.key_for(s.helper.agent_id)
    prompts = {f"{s.id}/{d}": _sha(system_prompt(s, d)) for s in scenarios for d in cfg["defenses"]}
    manifest = {
        "testbed_version": __version__,
        "git_commit": _git_commit(),
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "python": sys.version,
        "platform": platform.platform(),
        "config": cfg,
        "models": models,
        "conditions": {k: vars(v) for k, v in CONDITIONS.items()},
        "scenarios": {s.id: {"title": s.title, "goal": s.goal, "file": str(s.path.name if s.path else "")} for s in scenarios},
        "system_prompt_sha256_16": prompts,
        "trusted_public_keys": keyring.trust_store.to_dict(),
    }
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"manifest-{stamp}.json"
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def execute(
    cfg: dict,
    out_dir: Path | None = None,
    limit: int | None = None,
    progress: Callable[[str], None] = print,
    model_factory: Callable[[dict], object] = build_model,
) -> Path:
    scenarios = load_scenarios(resolve_path(cfg["scenarios_dir"]), cfg["scenarios"])
    out_dir = out_dir or resolve_path(Path("runs") / cfg["name"])
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "runs.jsonl"
    keyring = KeyRing(out_dir / "keys")

    # Build each model once up front so missing keys or placeholder ids fail before any run.
    described = [model_factory(m).describe() for m in cfg["models"]]
    write_manifest(out_dir, cfg, scenarios, keyring, described)

    grid = build_grid(cfg, scenarios)
    done = completed_run_ids(log_path)
    todo = [r for r in grid if r.run_id not in done]
    if limit is not None:
        todo = todo[:limit]
    progress(f"{len(grid)} runs in grid, {len(done & {r.run_id for r in grid})} already done, running {len(todo)}")

    lock = threading.Lock()
    state = {"consecutive_errors": 0, "aborted": False, "n": 0}

    def one(spec: RunSpec) -> dict:
        record = {
            "run_id": spec.run_id,
            "experiment": cfg["name"],
            "model_label": spec.model["label"],
            "provider": spec.model["provider"],
            "model": spec.model.get("model") or spec.model.get("behaviour"),
            "scenario": spec.scenario.id,
            "goal": spec.scenario.goal,
            "condition": spec.condition,
            "kind": spec.kind,
            "defense": spec.defense,
            "rep": spec.rep,
            "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        try:
            model = model_factory(spec.model)
            ep = run_episode(
                model, spec.scenario, get_condition(spec.condition), spec.kind, spec.defense, keyring, cfg["max_turns"]
            )
            record.update(status="ok", **ep)
        except HarnessError as exc:
            record.update(status="discarded", error=f"harness: {exc}")
        except ModelError as exc:
            record.update(status="model_error", error=str(exc))
        except Exception as exc:  # keep going; the record says what broke
            record.update(status="error", error=f"{exc.__class__.__name__}: {exc}", traceback=traceback.format_exc())
        return record

    def log(record: dict) -> None:
        with lock:
            with log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            state["n"] += 1
            if record["status"] == "ok":
                state["consecutive_errors"] = 0
                s = record["scores"]
                progress(
                    f"[{state['n']}/{len(todo)}] {record['run_id']}: "
                    f"proposed={int(s['proposed_unauthorized'])} executed={int(s['executed_unauthorized'])} "
                    f"legit={int(s['legit_success'])} "
                    f"tokens={record['usage']['input_tokens']:,}in/{record['usage']['output_tokens']:,}out"
                )
            else:
                state["consecutive_errors"] += 1
                progress(f"[{state['n']}/{len(todo)}] {record['run_id']}: {record['status'].upper()} {record.get('error', '')}")
                if state["consecutive_errors"] >= cfg["max_consecutive_errors"]:
                    state["aborted"] = True

    if cfg["workers"] <= 1:
        for spec in todo:
            if state["aborted"]:
                break
            log(one(spec))
    else:
        with ThreadPoolExecutor(max_workers=cfg["workers"]) as pool:
            futures = []
            for spec in todo:
                futures.append(pool.submit(lambda s=spec: None if state["aborted"] else one(s)))
            for fut in as_completed(futures):
                rec = fut.result()
                if rec is not None:
                    log(rec)
    if state["aborted"]:
        progress(
            f"Stopped after {cfg['max_consecutive_errors']} errors in a row. Fix the cause and rerun the same "
            "command: finished runs are skipped."
        )
    return log_path
