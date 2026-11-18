"""Command line: python -m testbed {validate,show,plan,run,report}."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .a2a import HelperAgent, verify_reply
from .conditions import CONDITIONS
from .models import ModelError
from .scenario import ScenarioError
from .envelope import build_envelope, serialise_envelope
from .prompts import system_prompt
from .report import load_runs, usage_by_model, write_report
from .runner import build_grid, execute, load_config, resolve_path
from .scenario import KINDS, load_scenarios
from .signing import KeyRing


def cmd_validate(args: argparse.Namespace) -> int:
    scenarios = load_scenarios(resolve_path(args.scenarios_dir))
    for s in scenarios:
        print(f"ok  {s.id}  {s.goal}  {s.title}")
    goals = {s.goal for s in scenarios}
    print(f"{len(scenarios)} scenarios, goals covered: {sorted(goals)}")
    return 0


def render_scenario(scenario_id: str, kind: str, defense: str, scenarios_dir: str) -> str:
    (sc,) = load_scenarios(resolve_path(scenarios_dir), [scenario_id])
    keyring = KeyRing()
    key = keyring.key_for(sc.helper.agent_id)
    out = [f"# {sc.id}: {sc.title} ({sc.goal}, {kind} reply, defense {defense})", ""]
    out += ["## System prompt (identical in every condition)", "", "```", system_prompt(sc, defense), "```", ""]
    out += ["## User message", "", "```", sc.user_task, "```", ""]
    for name, cond in CONDITIONS.items():
        helper = HelperAgent(sc, cond, kind, key)
        reply = helper.reply("T-0142", "(question from the coordinator)")
        result = verify_reply(reply, keyring.trust_store, "T-0142")
        env = build_envelope(cond, "T-0142", sc.helper.agent_id, sc.helper.role, reply.content, result)
        out += [f"## Condition {name}: {cond.description}", "", f"Verifier result (always run): {json.dumps(result.to_dict())}", ""]
        out += ["ask_helper result shown to the model:", "", "```json", serialise_envelope(env), "```", ""]
    return "\n".join(out)


def cmd_show(args: argparse.Namespace) -> int:
    print(render_scenario(args.scenario, args.kind, args.defense, args.scenarios_dir))
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    cfg = load_config(Path(args.config))
    scenarios = load_scenarios(resolve_path(cfg["scenarios_dir"]), cfg["scenarios"])
    grid = build_grid(cfg, scenarios)
    attack = sum(1 for r in grid if r.kind == "attack")
    print(f"experiment: {cfg['name']}")
    print(f"scenarios: {[s.id for s in scenarios]}")
    print(f"conditions: {cfg['conditions']}  defenses: {cfg['defenses']}  repetitions: {cfg['repetitions']}")
    print(f"models: {[m['label'] for m in cfg['models']]}")
    extra = sum(1 for r in grid if r.kind == "benign_extra")
    print(f"runs: {len(grid)} total, {attack} attacked, {extra} with a legitimate extra request, "
          f"{len(grid) - attack - extra} plain benign controls")
    per_model = len(grid) // max(1, len(cfg["models"]))
    measured: dict = {}
    if args.from_run:
        runs = [r for r in load_runs(Path(args.from_run) / "runs.jsonl") if r.get("status") == "ok"]
        measured = usage_by_model(runs, {m["label"]: m.get("price_per_mtok") for m in cfg["models"]})
        print(f"\nProjection from {len(runs)} finished runs in {args.from_run}:")
    else:
        print(f"\nRough estimate at an assumed {args.tokens_in:,} input and {args.tokens_out:,} output tokens per run"
              " (use --from RUN_DIR after a small test run for measured numbers):")
    for m in cfg["models"]:
        s = measured.get(m["label"])
        if s:
            tin, tout = s["mean_input_per_run"], s["mean_output_per_run"]
            src = f"measured over {s['runs']} runs"
        else:
            tin, tout = args.tokens_in, args.tokens_out
            src = "assumed" if not args.from_run else "assumed (no finished runs for this model yet)"
        line = f"  {m['label']}: {per_model} runs x ({tin:,.0f} in + {tout:,.0f} out) = {per_model * tin / 1e6:.2f}M in, {per_model * tout / 1e6:.2f}M out [{src}]"
        if s and s["cost_per_run"] is not None:
            line += f", about ${per_model * s['cost_per_run']:.2f} ({s['cost_source']})"
        else:
            p = m.get("price_per_mtok") or {}
            if p.get("input") is not None and p.get("output") is not None:
                line += f", about ${per_model * (tin * p['input'] + tout * p['output']) / 1e6:.2f} (config price_per_mtok)"
            else:
                line += ", cost unknown (set price_per_mtok)"
        print(line)
    if not args.from_run:
        print("Measure the real numbers with a small run first: python -m testbed run CONFIG --limit 5")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    cfg = load_config(Path(args.config))
    if args.workers:
        cfg["workers"] = args.workers
    out = Path(args.out) if args.out else None
    log_path = execute(cfg, out_dir=out, limit=args.limit)
    prices = {m["label"]: m.get("price_per_mtok") for m in cfg["models"]}
    md, csv_path = write_report(log_path, prices)
    print(f"\nLog: {log_path}\nReport: {md}\nSummary CSV: {csv_path}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir)
    prices: dict = {}
    manifests = sorted(run_dir.glob("manifest-*.json"))
    if manifests:
        cfg = json.loads(manifests[-1].read_text())["config"]
        prices = {m["label"]: m.get("price_per_mtok") for m in cfg["models"]}
    md, csv_path = write_report(run_dir / "runs.jsonl", prices)
    print(md.read_text())
    print(f"Written: {md}, {csv_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="testbed", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("validate", help="load and check all scenario files")
    v.add_argument("--scenarios-dir", default="scenarios")
    v.set_defaults(fn=cmd_validate)

    s = sub.add_parser("show", help="print exactly what the model sees for one scenario in conditions A-E")
    s.add_argument("scenario")
    s.add_argument("--kind", choices=list(KINDS), default="attack")
    s.add_argument("--defense", choices=["none", "D1", "D2"], default="none")
    s.add_argument("--scenarios-dir", default="scenarios")
    s.set_defaults(fn=cmd_show)

    pl = sub.add_parser("plan", help="count runs and estimate tokens/cost for a config")
    pl.add_argument("config")
    pl.add_argument("--tokens-in", type=int, default=8000)
    pl.add_argument("--tokens-out", type=int, default=1000)
    pl.add_argument("--from", dest="from_run", help="project from measured usage in an existing run directory")
    pl.set_defaults(fn=cmd_plan)

    r = sub.add_parser("run", help="run (or resume) an experiment")
    r.add_argument("config")
    r.add_argument("--limit", type=int, help="run at most N not-yet-finished runs")
    r.add_argument("--workers", type=int, help="parallel runs (overrides config)")
    r.add_argument("--out", help="output directory (default runs/<name>)")
    r.set_defaults(fn=cmd_run)

    rp = sub.add_parser("report", help="summarise a run directory")
    rp.add_argument("run_dir")
    rp.set_defaults(fn=cmd_report)

    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except (ModelError, ScenarioError, ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
